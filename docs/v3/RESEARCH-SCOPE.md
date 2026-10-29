# Recursion v3 research scope

Status: product research map, 2026-10-25. This page sequences product questions and
gates; it is not the research archive. All research output (notes, briefs, findings,
source tables, competitor or frontier writeups, research registers or indexes) goes
to [CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research)
as `submissions/<record_id>/`, through a PR in that repo, with a digest on the Linear
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
`docs/v3/prompts/` are legacy and frozen; `experiments/v3/` holds only runnable
product checks.

Supersedes the foundry-era work breakdown in
`docs/v3/research-lane/PLAN-20260927.md` for anything that depended on the construction foundry.
Non-normative until ratified: this page sequences research, it does not change product authority.

## What we are building (the architecture to eventually implement)

Recursion v3 is a provider-neutral agent platform. The accepted requirements are the product
specifications, not this page. Read only the slice a task needs:

| Question | Record |
|---|---|
| Product and first usable release | [PRODUCT](PRODUCT.md) |
| Accepted direction and Owner answers | [DECISION-REGISTER](DECISION-REGISTER.md) (OR-021..OR-039) |
| Kernel, Head, ARMs, recovery | [ARCHITECTURE](ARCHITECTURE.md), [STATE](STATE.md) |
| Planning, amendments, delegation | [AUTONOMOUS-PLANNING](AUTONOMOUS-PLANNING.md), [EXECUTIVES](EXECUTIVES.md) |
| Execution and routes | [EXECUTION-FABRIC](EXECUTION-FABRIC.md), [ROUTE-CAPABILITIES](ROUTE-CAPABILITIES.md) |
| Config, terminal, interaction | [CLI](CLI.md), [TERMINAL-RUNTIME](TERMINAL-RUNTIME.md), [INTERACTION-SURFACES](INTERACTION-SURFACES.md) |
| Context, plugins, security | [CONTEXT](CONTEXT.md), [PLUGINS-AND-EDITIONS](PLUGINS-AND-EDITIONS.md), [SECURITY](SECURITY.md) |
| Release and migration | [ACCEPTANCE](ACCEPTANCE.md), [CURRENT-AND-MIGRATION](CURRENT-AND-MIGRATION.md) |

The open questions that drive the research are in [RESEARCH-AGENDA](RESEARCH-AGENDA.md) (lanes
R1-R17) with reconstructed evidence in [R1-R17-EVIDENCE](R1-R17-EVIDENCE.md). Method and evidence
classes are fixed by [SCIENTIFIC-RESEARCH](SCIENTIFIC-RESEARCH.md).

## The research lane

Research means producing **evidence**, not running a service. Each task ends in one of three
states: a pinned source, a deterministic rerunnable gate, or an explicit `unknown` with a named
blocker. Generated output without independent verification is not progress.

Everything below is provider-free. It runs on a plain checkout with Python 3.11+ and Git.

### Startable now (gates already exist and pass)

| ID | Question | Gate | Last observed |
|---|---|---|---|
| T-01 | Are the v3 specs, handoff packets and fixtures intact at the pinned base? | `python -X utf8 -B docs/v3/construction/next-work-20260914/check_handoff.py` | PASS, 49 specs unchanged, 0 provider calls |
| T-02 | Do the M1A/M1B/M1C schemas, 71 fixtures and 17 test programs still satisfy their frozen contracts? | `schemas/control-plane/v3/<area>/materialize.py --check`, `scripts/tests/v3/<area>/materialize_fixtures.py --check`, then every `*.test.py` under `scripts/tests/v3/<area>/` | PASS: 6 materialization checks, 17 test programs |
| T-03 | Does the OR-032 recovery reservation ledger satisfy its guards and mutations? | `python -X utf8 -B experiments/v3/recovery-ledger-20260914/test_ledger.py` | PASS, 16 tests |
| T-04 | Does the DQ-16 delegated-Q bundle pass its static preflight and fake controls? | `python -X utf8 -B experiments/v3/delegated-q-coupling-20260914/test_contract.py` then `check.py --mode preflight` | preflight only; `--mode execute` is admission-gated |

The frozen 2026-09-13 orientation-repair gate, `tools/check_v3_orientation.py`, was removed in
CSL-573 (last copy at tag `pre-deletion-r1-20261029`). Use the per-task gate for new branches.

### Next, bounded (needs a new gate inside the packet's allowed paths)

- **T-05 DQ execution controller** — inert fake-reducer controller that validates admission before
  any scientific run. Gate to be created at
  `experiments/v3/dq-execution-controller-20260914/test_controller.py`.
- **T-11 external-packet privacy scanner** — hard floor before any packet leaves the local trust
  domain. Gate to be created at `tools/check_v3_packet_privacy.py`.
- **WS-8 R-lane SPECs** — one prospective SPEC + finite domain + falsifier per lane.
  Historical intakes remain under frozen `docs/v3/research-lane/<lane>/`. New research
  bodies go to csl-research `submissions/<record_id>/`; an inert product checker, if
  needed, stays under `experiments/v3/<lane>/`.

### Blocked, Owner-gated (do not start)

Scientific execution (DQ-16 `--mode execute`), live provider/host runs, spend or quota reset,
the P8 soak and any continuous service, language/store selection, OR-015 migration, publication
and merge. Each needs a fresh Owner decision and, where it would call a provider, a new reviewed
admission. Record the request in `state/PROPOSALS.md`; do not perform it.

## Task contract

Every research task carries:

- a parent goal (`goal-recursion-v2-execution-fabric` is the lineage parent; product target v3);
- an exact source commit and a clean worktree;
- explicit allowed paths and non-goals;
- one machine-checkable gate with deterministic pass/fail and zero provider calls;
- a named independent verifier (different session, different family where available) that reruns
  the exact candidate — an implementer's own PASS is never acceptance;
- an independent-verifier receipt in the Git common directory under
  `research-attestations/<gate>/<candidate>/`, which the gates in
  `experiments/v3/*/check.py`, [ACCEPTANCE](ACCEPTANCE.md) and
  [CONSTRUCTION-RUNBOOK](CONSTRUCTION-RUNBOOK.md) read to establish completion.
  A csl-research `submissions/<record_id>/` record (repo, 40-hex commit, path,
  claim, limitations) supplements that receipt and never replaces it.

If there is no gate or no verifier, stop and comment on the Linear issue. The Linear
issue comment is the canonical escalation; `state/INBOX.md` is optional. If a task can
be read two materially different ways, or needs a decision not in `state/DECISIONS.md`, escalate
instead of choosing.

## Operating flow for a research session

```text
# clean fetched worktree pinned to the reviewed base
git fetch --all --prune
# branch: csl-<N>-<slug>, optional username prefix (chaandansaini101/csl-<N>-<slug>)
# pr-lint matches CSL-[0-9]+ case-sensitively; a lowercase branch alone does not pass.
# The CSL-N key in the PR title satisfies the check.
git worktree add ../recursion-<task> -b csl-<N>-<slug> origin/master

# run the gate; capture the receipt externally
python -X utf8 -B <gate> --candidate <FULL_SHA>

# commit only inside the allowed paths, push the review branch, Lucas merges
```

The retired construction foundry (`tools/construction_swarm/`, `docs/v3/construction-swarm/`) is
**not** part of this flow. Its code and records are in git history only. There is no standing
worker fleet; any future fleet needs its own reviewed control-plane decision.
