# Recursion writer sandbox plan (draft)

Drafted 2026-10-24 for [CSL-477](https://linear.app/csl-laboratories/issue/CSL-477). This file is a proposal. It does not accept a control, un-pause a writer, or author a workflow. Lucas decides.

**Non-goals.** No `.github/workflows/**` or `scripts/ci/**` edits in the PR that adds this file. Do not touch [recursion#285](https://github.com/CSL-Laboratories/recursion/pull/285) or #226.

## Why this exists

Cockpit is landing a hosted-runner sandbox under [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467) and [CSL-474](https://linear.app/csl-laboratories/issue/CSL-474) (P1–P10, conditions C1.1–C10.2). Recursion's Claude writers stay paused because Recursion has no matching plan. Recursion B′ ([CSL-414](https://linear.app/csl-laboratories/issue/CSL-414), [recursion#285](https://github.com/CSL-Laboratories/recursion/pull/285)) already splits prepare / model / publish so the model has no write token. That split is necessary and not sufficient: cockpit still needed `strictAllowlist`, `Edit` denies on protected runner paths, and a hosted U-8 proof before treating the sandbox as real.

## Sources (not restated as Recursion decisions)

| Source | What it is | Evidence class |
|---|---|---|
| [CSL-474 comment `2f7394f1`](https://linear.app/csl-laboratories/issue/CSL-474#comment-2f7394f1-07fd-43bf-9261-203270c0cdbc) | Lucas, 2026-10-24 13:22 PT: approve P1–P10 with C1.1–C10.2 for the cockpit E3/E2/E1 stack | Founder decision on cockpit |
| [CSL-467](https://linear.app/csl-laboratories/issue/CSL-467) / cockpit [#256](https://github.com/CSL-Laboratories/ai-investment-cockpit/pull/256) | `persist-credentials: false`, `sandbox.network.allowedDomains` + `strictAllowlist: true`, `autoAllowBashIfSandboxed: false`, `Edit` (not `Write(path)`) denies on `_runner_file_commands` and `//home/runner/work/_actions` | Source-documented on cockpit; Recursion has not copied it |
| Cockpit U-8 run [40133359151](https://github.com/CSL-Laboratories/ai-investment-cockpit/actions/runs/40133359151) on disposable [#266](https://github.com/CSL-Laboratories/ai-investment-cockpit/pull/266) | (a) helper-only write path **pass** (model side; App token unavailable so no real helper push). (b) Edit deny **inconclusive**. (c) unlisted-host refuse **inconclusive**. Samir accepted (b)/(c)/real helper push as a **post-merge check** on the first real fixer/mention run | Locally observed on cockpit hosted runners; does not prove Recursion |
| Recursion `master` `1fea0b0b` (includes [#284](https://github.com/CSL-Laboratories/recursion/pull/284) and the CSL-473 pin from [#287](https://github.com/CSL-Laboratories/recursion/pull/287)) | Combined writer jobs; CLI 2.1.306 at `/opt/csl-claude/2.1.306/claude`; model still has `contents: write` and `git push` | Locally observed on this tree |
| Recursion [#285](https://github.com/CSL-Laboratories/recursion/pull/285) `d7346aab` (read-only; this plan does not edit that branch) | Prepare / model / publish; `persist-credentials: false`; helper-only Bash; trusted publisher + outbox; publisher loaded from a default-branch SHA | Source-documented on that PR |

[CSL-377](https://linear.app/csl-laboratories/issue/CSL-377) stays **not Qualified**. Merging #285 does not activate Recursion writers.

## Recursion writer surface

Three jobs can still change a PR branch. Pause levers today: PR label `claude:pause` or `hold` (any case; unreadable labels count as paused), and repository variable `CLAUDE_FIXER_PAUSE=true`. Agents must not remove a pause.

| Writer | Workflow on `master` `1fea0b0b` | After #285 (proposed, not merged) |
|---|---|---|
| `@claude` mention | `claude.yml` `claude-mention`: one job, `contents: write`, checkout **without** `persist-credentials: false`, `Bash(git push …)` allowed | `mention-prepare` (read) → `claude-mention` (read, `persist-credentials: false`) → `mention-publish` (`id-token: write`; App token after bundle validation) |
| Devin fixer | `claude.yml` `devin-review-fix`: same combined shape | `fixer-prepare` → `devin-review-fix` → `fixer-publish` |
| Conflict merge | `conflict-scan.yml` `conflict-resolve`: combined; broader Edit/Write denies than mention/fixer | Reusable `claude-conflict-writer.yml` with the same split |

CLI pin on both trees: `anthropics/claude-code-action@a5a423f75c251dcbff8a74e99f83b2cb03c72bfa` (v1.0.256) with `path_to_claude_code_executable: /opt/csl-claude/2.1.306/claude` and `DISABLE_UPDATES=1`. Stay on 2.1.306 until [CSL-476](https://linear.app/csl-laboratories/issue/CSL-476) (P7/C7.2).

#285 already makes the trusted publisher and outbox the only GitHub write path for a successful run. Until #285 lands, publish loads `scripts/ci/claude_fixer_publish.py` from a recorded default-branch SHA and **refuses** because that blob is not on `master` yet (Samir, CSL-414 comment `72d408ec`). That fail-closed state is expected; it is not a sandbox.

## P1–P10 mapped onto Recursion

Cockpit P1–P10 (CSL-474 `2f7394f1`) with each condition named. Recursion status is observed, not accepted.

| Control | Cockpit meaning | Recursion today | Recursion after #285 | Recursion still owes |
|---|---|---|---|---|
| **P1 / N1** C1.1–C1.4 | `--setting-sources user` is not coverage of `AGENTS.md` or cwd Reads. Extra fail-closed on `settings.local.json`. Probe 2 confirms 2.1.306 does not load cwd hooks. Record as a D10 amendment; do not edit frozen DESIGN files | Combined jobs have no `--setting-sources user` | `--setting-sources user` is on mention and fixer `claude_args`. Prompt still says to Read `AGENTS.md` / `CLAUDE.md` when needed | C1.1 Recursion record (not a silent copy of cockpit DESIGN). C1.2 `settings.local.json` fail-closed. C1.3 do not claim AGENTS.md is covered. C1.4 / P9 Probe 2 on a Recursion hosted runner |
| **P2 / N9** C2.1–C2.2 | bun never on the CLI PATH. `model-api` is the only GitHub write path for the model | Model Bash includes `git push`, `gh pr view/diff`, and `tools/ci_gate.py`. Combined job has write credentials | Helper-only `Bash(python3 …/model-api --:*)`. `git push` / `git config` / `gh api` disallowed. Publish job is the only pusher | Confirm bun is off PATH on Recursion model jobs (C2.1). Keep helper-only (C2.2) when adding the sandbox so `autoAllowBashIfSandboxed` cannot widen Bash |
| **P3 / N8** C3.1–C3.3 | Exact Edit+Write workspace tools. Do **not** add `Write(path)` denies (they never match). Use `Edit(//abs/…)`. U-8 hosted proof still owed | Mention/fixer: relative `Edit`/`Write` on workflows, pip, `.git/**`, guard dir. Conflict writer: extra relative denies including `scripts/ci/**` and `tools/ci_gate.py`. No `//` form, no `_runner_file_commands` | Same relative deny list plus `.claude/**` and `~/.gitconfig`. Still no `//` form, no file-command denies, no `sandbox:` object | Port cockpit #256 sandbox + `Edit(//…)` file-command/`_actions`/`externals` denies. Drop inert `Write(path)` denies. Recursion U-8 (C3.3) |
| **P4 / G9/F7** C4.1–C4.3 | Fix `kill_remaining_children()` zombie counting; behavioural N14 test; no cgroup leaf in this stack | No E3 launcher | No E3 launcher | Only if Recursion ports E3. Not in the first Recursion sandbox PR. Do not invent a cgroup |
| **P5** C5.1–C5.3 | `first_bash=missing` is attribution (E4 parked). Accept `sudo_fork` and `sudo_exec` only. P9 records the live parent chain | Unmeasured | Unmeasured | Only with E1. E4 stays parked |
| **P6** C6.1–C6.4 | `pluginConfigs`: `agents-md@builtin` instructionFiles `claude-md` only. Restore/stub `AGENTS.md` like `CLAUDE.md`. Remove prompt invitation to Read them. Fail closed if a managed MCP appears | Prompt tells the model to follow `AGENTS.md` | Prompt: "Follow AGENTS.md, CLAUDE.md and CODEOWNERS; they are not preloaded, so Read them when you need them" | C6.1–C6.3 on Recursion settings JSON + prompt. C6.4 fail-closed on managed MCP |
| **P7** C7.1–C7.2 | Stay on 2.1.306 while `/opt/csl-claude/mcp.empty.json` is root-owned 0444 and the launcher discards foreign `--mcp-config`. Follow-up pin 2.1.310 is CSL-476, not this stack | 2.1.306 binary pin + `path_to` (CSL-473). No empty MCP file, no launcher argv rewrite | Same pin on the split model jobs | Keep 2.1.306 until CSL-476. If Recursion ports E3, C7.1 applies. Do not jump to 2.1.310 here |
| **P8** C8.1–C8.4 | Cockpit: #262 and #256 on main, then rebuild E3→E2→E1 linearly. No rebase/force-push | n/a | n/a | Recursion order is below. Do not replay cockpit SHAs onto Recursion |
| **P9** (reduced) | Probe 0: host facts. Probe 1: N14 native test in CI. Probe 2: 2.1.306 CLI, immutable empty MCP, `--setting-sources user` loads no cwd hooks. Probes 0/2 need a new `workflow_dispatch` and Lucas's yes; Probe 2 needs a second yes if it uses the OAuth token | No probes | No probes | Recursion U-8 canary is the first hosted proof. Probes 0/2 only if Lucas admits a Recursion dispatch workflow |
| **P10 / I-4** C10.1–C10.2 | 0b sweep is record-only. Invariant: 0b runs before the `head_sha` checkout | Unmeasured | Unmeasured | If Recursion ports E3: record-only, before checkout |

## Protected paths

Three layers. They are not the same set. The publisher is the write barrier after #285; Edit denies are defence in depth and do not contain Edit/Write (those tools run outside bwrap).

### 1. `CONTROL_PLANE.md` (governance)

`CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`, `.env`, credential/auth configuration, `scripts/ci/**`, `scripts/tests/**/fixtures/**`, `tools/ci_gate.py`. Changing an existing assertion still needs a named human GitHub review. Fixtures under `experiments/` are **open** (not decided).

### 2. Model `permissions.deny` (Claude Code)

On `master` mention/fixer: `Read(.env/.env.*)`, `Edit`/`Write` on `.github/workflows/**`, `pip/**`, `*.pth`, `sitecustomize.py`, `usercustomize.py`, `.git/**`, the guard dir. **Missing vs CONTROL_PLANE:** `CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`, `scripts/ci/**`, fixtures, `tools/ci_gate.py`. Conflict-scan already denies `.github/**`, `scripts/ci/**`, `tools/ci_gate.py`, `AGENTS.md`, `CLAUDE.md`, `CODEOWNERS`.

Cockpit #256 additionally denies (Edit, `//` form where needed): `$RUNNER_TEMP/_runner_file_commands/**` and `set_env_*` / `add_path_*`, `//home/runner/work/_actions/**`, `//home/runner/runners/*/externals/**`, `~/.bashrc` / `~/.profile` / `~/.bash_profile` / `~/.bash_login`, `Bash(dangerouslyDisableSandbox:true)`. Recursion has none of those.

Proposed Recursion deny set (implementation PR, not this file): cockpit's runner/file-command/`_actions`/`externals` Edit denies **plus** Edit denies for the CONTROL_PLANE globs and for `.github/**` (not only `workflows/**`). Use `Edit(//abs/…)` for runner paths. Do not add `Write(path)` denies (P3/C3.2). Workspace Edit/Write stay allowed only under `${{ github.workspace }}/**` (cockpit D10).

### 3. Trusted publisher `is_protected` (#285, not on `master`)

From `scripts/ci/claude_fixer_publish.py` on #285 (read-only): casefolded refuse on any path component starting `.git` or `.env`; dirs `.claude`, `.claude-fixer-out`, `.claude-fixer-replies`, `.cursor`; names `CODEOWNERS`, `AGENTS.md`, `CLAUDE.md`, `.mcp.json`; files `CONTROL_PLANE.md`, `ANCHOR.md`, `state/DECISIONS.md`, `tools/ci_gate.py`, `tools/test_ci_gate.py`; prefixes `tools/test_claude_`, `tools/test_codex_`, `tools/test_fixer_`, `tools/test_stuck_`; `scripts/ci/**`; `scripts/tests/**/fixtures`; `pip/**`. Also gitlinks (`160000`), new executables, >300 paths, >5 MB.

That list is the only write-time enforcement after #285. Align the model's Edit denies with it so a refused publish is not the first time a protected edit is noticed. Do not treat the deny list as a substitute for the publisher.

## Allowed network hosts

Cockpit model sandbox (`sandbox.network`, `strictAllowlist: true`):

- `api.github.com`
- `github.com`

That is the path `model-api`'s `gh api` / GraphQL uses. Unlisted hosts must be refused. Recursion #285 has **no** `sandbox.network` object, so there is no allowlist and no strict refusal.

Proposed Recursion list for the **model job** (all three writers): the same two hosts, `strictAllowlist: true`. Do not add `downloads.claude.ai`, `claude.ai`, or PyPI to the **model** allowlist. The pinned 2.1.306 install and any `pip` in a trusted step run **before** the model, as `runner`, and are outside this allowlist.

Also set, matching cockpit #256:

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

Wiring tests that pin YAML strings are not enforcement. Cockpit U-8 is the pattern Recursion must copy, on Recursion runners, with Recursion's `claude.yml`.

| ID | Control | What counts as proof | What does not |
|---|---|---|---|
| **R-U8-a** | Helper-only write path | Hosted mention or fixer run: model checkout has `persist-credentials: false`; model does not push; `*-publish` is the only publisher; after #285 is on `master`, one run shows a real App-token fast-forward (cockpit U-8 (a) did not, because the App token is unavailable before merge) | Offline B′ packet; string pins; cockpit run 40133359151 |
| **R-U8-b** | Edit deny | Hosted run where the model attempts Edit/Write on a protected canary (`.git/config` or `_runner_file_commands/set_env_*` or a CONTROL_PLANE path) and the denials report shows the deny; canary bytes unchanged | Deny list present in settings; `claude-denials: count=0` (cockpit U-8 (b) was inconclusive for this reason) |
| **R-U8-c** | Unlisted host + `strictAllowlist` | Hosted run where the model (or a canary) fetches a host not in `{api.github.com, github.com}` and is refused | Network object present in settings with no fetch attempted |
| **R-U8-d** | CLI pin | Job log shows `/opt/csl-claude/2.1.306/claude` and `DISABLE_UPDATES=1`; no `curl …/install.sh \| bash` | Action SHA pin alone (CSL-473) |
| **R-P1** | `--setting-sources user` / no cwd hooks | P9 Probe 2 on Recursion, or a hosted log that 2.1.306 did not load cwd hooks/`settings.local.json` | Flag present in `claude_args` |
| **R-P6** | AGENTS.md plugin | Settings JSON with `pluginConfigs` `claude-md` only, plus a hosted check that `AGENTS.md` was not injected as instructions | Prompt wording |
| **R-P4/P5/P10** | E3/E1 leftovers | Only if Recursion ports that stack; Probe 0/1 then apply | Not required for the first sandbox PR |

Cockpit accepted U-8 (b)/(c) and a real helper push as a **post-merge check** on the first real run after #256/#258 (Samir on #256). Recursion should **not** inherit that waiver without Lucas saying so (open question 2).

Recommended Recursion canary (implementation phase, founder-triggered): a disposable draft PR stacked on the sandbox branch, with a genuine Devin `kind: bug` or `workflow_dispatch`, canaries in a normal file **and** a denied path. Do not merge the canary. Do not dispatch from this docs PR.

## Rollout relative to #285

Lucas's Recursion merge order (2026-10-24 13:52 PT, CSL-414): **#284 then #285**. #284 is on `master` (`1fea0b0b`). #285 is open, Samir-approved on `d7346aab`, waiting on the remaining requested review and Lucas's merge. Activation and CSL-377 qualification stay separately gated.

Proposed sequence (docs now, implementation later):

1. **This draft PR** (`docs/sandbox-plan.md` only). Can land before or after #285. Does not un-pause anyone.
2. **Merge #285** when founders merge it. Writers remain paused. Publish starts working once the publisher blob is on `master`, which is why the pause must stay until the sandbox exists.
3. **Do not un-pause on #285 alone.** Combined-job `master` still has write credentials; #285 without cockpit CSL-467 controls still lacks `strictAllowlist` and file-command Edit denies.
4. **Wait for cockpit #256/#258** (and Lucas's go) so Recursion copies a reviewed sandbox object, not a moving draft. The CSL-474 E3/E2/E1 stack is a **later** Recursion port unless Lucas picks option A in the questions below.
5. **Recursion sandbox implementation PR** (new issue, workflow-edit authoring session): copy the sandbox object, `Edit(//abs)` denies, helper-only already from #285, `autoAllowBashIfSandboxed: false`, P6 settings, post-Claude `env -i` pins from cockpit #256. Allowed paths: `.github/workflows/claude.yml`, `claude-conflict-writer.yml` / `conflict-scan.yml`, tests. Needs a recorded founder authoring approval because it edits workflows.
6. **Hosted R-U8** on that implementation head (disposable canary). Then Lucas un-pauses.

Never rebase or force-push. If #285 moves, merge `master` into the sandbox implementation branch.

## Exact conditions for un-pausing the writers

All of the following. Missing any one keeps the pause. A merge, a green `linux` check, or this document reaching `master` is not an un-pause.

1. **Lucas posts the un-pause from his own account** (or a developer-approved comment that meets `CONTROL_PLANE.md` D-043) on [CSL-477](https://linear.app/csl-laboratories/issue/CSL-477) or [CSL-414](https://linear.app/csl-laboratories/issue/CSL-414), naming the Recursion head SHA that is allowed to run. Agent comments cannot un-pause.
2. **#285 is merged** to `master`, so the publisher blob exists on the default branch and the model job has no write token / no `git push`.
3. **The Recursion sandbox implementation is merged** to `master` (step 5 above), including `persist-credentials: false` (from #285), `sandbox.network` + `strictAllowlist: true`, `autoAllowBashIfSandboxed: false`, `allowUnsandboxedCommands: false`, `failIfUnavailable: true`, and `Edit(//abs)` denies for `_runner_file_commands` and `/home/runner/work/_actions`.
4. **R-U8-a is observed** on a Recursion hosted run of that merged workflow (model did not push; publish is the only writer). R-U8-b and R-U8-c are either observed on that run **or** Lucas explicitly accepts them as a post-merge first-run check the way Samir did for cockpit #256.
5. **CLI remains 2.1.306** with `path_to_claude_code_executable` (R-U8-d), unless CSL-476 has landed its own signed pin and Lucas has moved the pin.
6. **A human** unsets `CLAUDE_FIXER_PAUSE` (or sets it to anything other than exactly `true`) **and** removes `claude:pause` / `hold` from the PRs that should run. Agents never remove those.

Until then: leave the repo variable and labels in place. Do not dispatch `claude.yml`. Do not add `ci:full` to force a writer.

## Open questions for Lucas

1. **Depth of the first Recursion sandbox PR.** A) Copy only the CSL-467 sandbox object + Edit denies onto post-#285 writers (recommended: smallest reversible step). B) Wait until cockpit E3/E2/E1 ([CSL-474](https://linear.app/csl-laboratories/issue/CSL-474) #264/#263/#265) merge, then port that stack. C) Port E3/E2/E1 in parallel with cockpit.

2. **R-U8-b/c timing.** A) Require a hosted Recursion canary that actually trips the Edit deny and an unlisted-host refusal **before** un-pause (recommended; cockpit U-8 (b)/(c) were inconclusive). B) Same waiver as cockpit: accept (b)/(c) as a post-merge check on the first real fixer/mention run, revert or hotfix if it fails.

3. **`ubuntu-latest` 24.04 → 26.04** (GitHub window 2026-10-19 to 2026-11-19). A) Pin `ubuntu-24.04` on writer jobs until Probe 0 exists. B) Accept 26.04 and run R-U8 on both images.

4. **Model Edit denies vs publisher `is_protected`.** A) Add CONTROL_PLANE + `scripts/ci/**` + `tools/ci_gate.py` to the model deny list (recommended, aligned with conflict-scan). B) Leave those to the publisher only.

5. **CSL-476 (2.1.310).** A) Stay on 2.1.306 until Recursion R-U8 passes (recommended, P7/C7.1). B) Move Recursion with cockpit.

6. **Passwordless sudo.** #284 closed pip-under-sudo on Recursion. Drop sudo immediately before the model action, as CSL-473 listed as optional?

7. **P9 Probe 0/2 `workflow_dispatch`.** Admit a Recursion-only probe workflow (needs your yes, and a second yes if Probe 2 needs `CLAUDE_CODE_OAUTH_TOKEN`)? Or skip probes until after un-pause?

8. **Does accepting this document un-pause anyone?** Recommendation: **no.** Acceptance only unblocks an implementation issue.

Meanwhile: this PR stays draft; writers stay paused; #285 is not touched.
