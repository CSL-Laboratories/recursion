# Recursion v3 development entry point

Reviewed 2026-09-13 against GitHub master `797be4f18edf3f9e0f2dbac9432f190dbe1ed969`.
This is a source orientation snapshot, not live machine status or a new operating policy.
Lucas's current request is to repair repository orientation and verify the v3 specifications.

The former construction foundry (the WSL Devin/SWE-2 harness under `tools/construction_swarm/`
and its specs under `docs/v3/construction-swarm/`) was **retired on 2026-09-28**. It is not
part of the v3 product and not part of the current workflow. Its code, specs and campaign
records remain recoverable from git history. Do not restart it.

Research starts at [RESEARCH-SCOPE](RESEARCH-SCOPE.md), which maps the goals to the
[RESEARCH-AGENDA](RESEARCH-AGENDA.md) and to gates that already exist and pass.

Read [CONTROL_PLANE](../../CONTROL_PLANE.md) first, then [ANCHOR](../../ANCHOR.md),
[AGENTS](../../AGENTS.md), and this page. The current development target is **Recursion v3**.
V1/v2 remain historical implementation and regression references. Do not infer a v2
development task from a legacy filename, goal identifier, or acceptance command.

For shared research, use the [CSL research index](https://github.com/CSL-Laboratories/csl-research/blob/main/INDEX.md),
[Recursion context](https://github.com/CSL-Laboratories/csl-research/blob/main/knowledge/recursion/README.md)
and [research-to-delivery guide](https://github.com/CSL-Laboratories/csl-research/blob/main/README.md#connecting-research-and-delivery).
These views are dated explanatory navigation; linked submissions are candidate
evidence. They do not imply that all research has migrated. Check cited revisions
and current product instructions before dependent work. Linear remains the live
source for ownership, status and admission. Product contracts and authenticated
human decisions retain their authority; operational contracts and runbooks
that this repository's code, a gate or CI reads stay here, and everything
else non-public goes to csl-research. A file stays in this repository if
code, a gate or CI here reads it.

## Specification and authority

The product requirements and accepted Owner decisions remain intact. They describe a
provider-neutral agent platform, with native execution, supported upstream harnesses,
combined workflows, persistent assistants, configurable communication and delegation,
terminal controls, plugins, context/compaction, and Windows/Linux operation and recovery.
The dedicated graphical application follows the initial terminal platform.

Amendment proposed 2026-10-24 (acceptance is a human decision by Lucas): GUI timing in the
sentence above is already superseded by D-038 (built but opt-in; no capability deferred).
This amendment does not reopen D-038. **Beyond D-038** see
[AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md) (daemon-served Tauri shell, standalone
app, "goat" bar) and [PRODUCT.md](PRODUCT.md). Historical research-lane snapshots that
still say "GUI later" are not rewritten. `ANCHOR.md` is not edited.

Read only the specification slices needed for the assigned task:

| Question | Authoritative product record |
|---|---|
| Product and first usable release | [PRODUCT](PRODUCT.md) |
| 2026-10-24 proposed amendment (unaccepted) | [AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md) |
| Research / open questions (F-1…F-15) | `CSL-Laboratories/csl-research` commit `250fdbbe2964e4889096a487e2dfb6d7794046a6`, path `submissions/20261025-recursion-csl494-rp0-amendment-research-01/` (candidate evidence; vault PR merged) |
| Accepted direction and resolved Owner questions | [DECISION-REGISTER](DECISION-REGISTER.md), especially OR-021 through OR-039 |
| Kernel, Head, ARMs and recovery | [ARCHITECTURE](ARCHITECTURE.md), [STATE](STATE.md) |
| Task planning, amendments and delegation | [AUTONOMOUS-PLANNING](AUTONOMOUS-PLANNING.md), [EXECUTIVES](EXECUTIVES.md) |
| Native/upstream execution and route evidence | [EXECUTION-FABRIC](EXECUTION-FABRIC.md), [ROUTE-CAPABILITIES](ROUTE-CAPABILITIES.md) |
| Configuration, terminal and communication | [CLI](CLI.md), [TERMINAL-RUNTIME](TERMINAL-RUNTIME.md), [INTERACTION-SURFACES](INTERACTION-SURFACES.md) |
| Context, plugins and security | [CONTEXT](CONTEXT.md), [PLUGINS-AND-EDITIONS](PLUGINS-AND-EDITIONS.md), [SECURITY](SECURITY.md) |
| Release obligations and migration | [ACCEPTANCE](ACCEPTANCE.md), [CURRENT-AND-MIGRATION](CURRENT-AND-MIGRATION.md) |

Hermes-led repository control was retired on 2026-09-28 (`state/DECISIONS.md` D-017); Hermes is an
optional upstream integration, not the executive control plane. The
[constitutional amendment proposal](CONSTITUTIONAL-AMENDMENT-PROPOSAL.md) is historical. Do not
reopen answered Owner questions or select a product language, store, framework, spending policy,
or autonomous operating mode from a research recommendation.

## What exists and what remains

| Layer | Source-backed state | Limits / next prerequisite |
|---|---|---|
| V1/v2 (retired) | Removed 2026-09-28; preserved at tag `pre-v3-clean-cut-20260928` | Not a dependency of v3 and not part of this repository's current scope. |
| V3 M1A | Planning schema, validation, literal fixtures and in-memory replay | Pure/inert scope; preserve frozen contracts. See [campaign](M1A-CONSTRUCTION-CAMPAIGN.md). |
| V3 M1B / M1C | Execution and integrity schemas, fixtures and test oracles | These specify behavior; they do not prove live adapters or physical persistence. See [M1B](M1B-CONSTRUCTION-CAMPAIGN.md), [M1C](M1C-CONSTRUCTION-CAMPAIGN.md). |
| Later v3 experiments | Composition, reservation, wire, consultation, source/publication and bounded comparison work | Full formal comparison, physical recovery and common-writer integration remain open. |

The 2026-09-08 through 2026-09-14 formal and coupled comparison experiments added bounded
evidence to the later v3 experiments above. Full formal comparison, physical recovery and
common-writer integration remain open, and those campaign deadlines have expired.

PRs 124, 125 and 127 are ancestors of the inspected master. Statements that those PRs
are open, or that a campaign is running, are dated historical observations.
The 2026-09-12 census recorded later reservation settlement, rate limits and malformed
filings; it never established current quota, process state or accepted scientific results.

The chronological [ROADMAP](ROADMAP.md) preserves earlier next-step proposals. Its
M1A-era instructions and the old [documentation gate](VALIDATION.md) are not instructions
to restart completed work. No historical clock, quota observation or authorization
renews merely because an agent opens a handoff.

## Starting a bounded development task

Use a fresh fetched, clean review worktree and record its full commit. Name the review
branch `csl-<N>-<slug>`, optionally prefixed with a GitHub username
(`chaandansaini101/csl-<N>-<slug>`). Put `CSL-N` in the PR title. pr-lint matches
`CSL-[0-9]+` case-sensitively, so a lowercase branch name alone does not pass. The
`CSL-N` key in the PR title satisfies the check.

On Windows, this repository has paths exceeding 260 characters; use
`git -c core.longpaths=true` for checkout and status/diff operations when needed.
This is a per-command option.

Before inference or writes, bind the task to:

- the parent goal, v3 objective, source commit and relevant accepted requirements;
- exact allowed paths, non-goals and prerequisite evidence;
- the acceptance commands and required independent review;
- provider/tool permissions, finite budget, deadline, retry and stopping conditions;
- durable checkpoint and result locations, including failures and uncertainty.

The existing parent [goal-recursion-v2-execution-fabric](../../state/goals/goal-recursion-v2-execution-fabric.md)
is also the recorded parent of the v3 construction campaigns. Its legacy identifier
is preserved for lineage; it neither changes a task's v3 target nor authorizes a new campaign.
The current [orientation repair task](../../state/tasks/v3-orientation-20260913.md) has a
separate, source-only gate. A product implementation packet must identify its own gate.

Research and construction are driven from the goals and experiments directly, not through a
standing swarm. Start at [RESEARCH-AGENDA](RESEARCH-AGENDA.md) for the open questions.
All research output (notes, briefs, findings, source tables, competitor or frontier
writeups, research registers or indexes) goes to
[CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research) as
`submissions/<record_id>/`, through a PR in that repo, with a digest on the Linear
issue. Never commit research content to this repo; this repo only links to the
vault record (repo, 40-hex commit, path, claim, limitations). New non-public docs
(research, specs, decisions, evidence and receipts, gate artifacts, and runbooks
that aren't public) go to that vault through a PR there, except operational
contracts and runbooks that this repository's code, a gate or CI reads: those
stay here. A file stays in this repository if code, a gate or CI here reads
it; everything else non-public goes to the vault. Existing files stay;
CSL-536 will move them. The existing decision logs (`state/DECISIONS.md` and `docs/v3/DECISION-REGISTER.md`) and their founder-approved authoring process are unchanged. The only other new docs allowed here are public knowledge-base,
README, setup and implementation instructions for outside readers, and agent
instruction files. If
csl-research is not accessible, stop and post a blocker on the Linear issue; do not
fall back to this repo. `docs/v3/research-lane/`, `docs/v3/foundation-research/` and
`docs/v3/prompts/` are legacy and frozen; [experiments/v3/](../../experiments/v3/)
holds only runnable product checks. Any future worker fleet requires its own reviewed
control-plane decision before use.

Deterministic process, deadline, quota and receipt checks should provide continuous
awareness. Model supervision is advisory and should respond to actionable events.
A worker's self-report cannot replace independent verification. The existing supervisor
adds no background scheduler. Terra, Hermes, DeepSeek or another route is a future
qualification choice, not selected or enabled by this repair. Compaction must retain the
task's source, authority, budget, gate, checkpoint and unresolved evidence references.

## Reproducible orientation and specification checks

The frozen 2026-09-13 orientation-repair gate, `tools/check_v3_orientation.py`, was
removed in CSL-573. It failed on any change after that repair by design and no CI job ran
it. The last copy is at commit `00004ef59b01`. For current checks, run
`python -X utf8 -B tools/ci_gate.py` from the repository root.
