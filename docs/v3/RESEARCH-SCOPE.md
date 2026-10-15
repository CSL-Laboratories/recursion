# Recursion v3 research scope

Status: active scope, 2026-09-28. Supersedes the foundry-era work breakdown in
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

`tools/check_v3_orientation.py` is the frozen 2026-09-13 orientation-repair gate: it binds to that
repair's allowed-path scope and fails on any later change by design. Do not use it as a general
check for new branches; use the per-task gate instead.

### Next, bounded (needs a new gate inside the packet's allowed paths)

- **T-05 DQ execution controller** — inert fake-reducer controller that validates admission before
  any scientific run. Gate to be created at
  `experiments/v3/dq-execution-controller-20260914/test_controller.py`.
- **T-11 external-packet privacy scanner** — hard floor before any packet leaves the local trust
  domain. Gate to be created at `tools/check_v3_packet_privacy.py`.
- **WS-8 R-lane SPECs** — one prospective SPEC + finite domain + falsifier per lane, under
  `docs/v3/research-lane/<lane>/` with an inert checker under `experiments/v3/<lane>/`.

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
- an external receipt directory under `research-attestations/<campaign>/<candidate>/`.

If there is no gate or no verifier, stop and record a blocker in `state/INBOX.md`. If a task can
be read two materially different ways, or needs a decision not in `state/DECISIONS.md`, escalate
instead of choosing.

## Operating flow for a research session

```text
# clean fetched worktree pinned to the reviewed base
git fetch --all --prune
# branch: csl-<N>-<slug>, optional username prefix (chaandansaini101/csl-<N>-<slug>)
git worktree add ../recursion-<task> -b csl-<N>-<slug> origin/master

# run the gate; capture the receipt externally
python -X utf8 -B <gate> --candidate <FULL_SHA>

# commit only inside the allowed paths, push the review branch, Lucas merges
```

The retired construction foundry (`tools/construction_swarm/`, `docs/v3/construction-swarm/`) is
**not** part of this flow. Its code and records are in git history only. There is no standing
worker fleet; any future fleet needs its own reviewed control-plane decision.
