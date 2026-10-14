---
title: Capability-composition conformance suite (provider-free)
status: experiment-in-progress
last-updated: 2026-10-02
---
# Capability-composition conformance suite

Provider-free, synthetic conformance fixtures for the capability-composition candidates, authorized
by **D-037** (an already-installed runtime — **Python** — as a disposable test harness only; the
product implementation language remains open, FU-01). Target candidates:

- **R-PC** `CompositionManifest` + the eight-pass compiler — fixtures **F01–F18**
  (`docs/v3/research-lane/capability-composition-20261002d/rpc/report.md`).
- **R-UI** `recursion.ui-contract@candidate-2` — fixtures **UI-01…UI-20**
  (`.../rui/report.md`).
- **R-MOB** mobile candidates — fixtures **MOB-F01…F24**, plus the shared client semantic matrix
  **MP01…MP11** (`.../rmob/report.md`, `docs/v3/research-lane/ios-android-20261002/`).

## Evidence boundary

- **Synthetic and provider-free:** no network, model, provider, account, credential, device, store or
  live-host action. Fixtures run in-process over literal inputs.
- **An implementer's PASS is not acceptance.** Every fixture needs a valid **positive control**, an
  **adverse mutation**, and an **independent oracle** that observes the actual submitted bytes /
  records / effects and does not trust candidate flags. A missing required observation is
  `HOLD_UNKNOWN`, never a pass.
- **What this does not prove:** structural conformance within the bounded synthetic corpus only. It
  does not establish model quality, latency, resource, cache, security, injection resistance,
  platform isolation, external-service qualification, store eligibility or porting cost.

## Layout

| Path | Role |
|---|---|
| `FIXTURE-INDEX.md` | Reconciled fixture registry (all lanes) and per-fixture status |
| `check_suite.py` | Structural gate: IDs present, sources present, public-scope/secret scan |
| `fixtures/` | Fixture definitions (added per lane) |
| `run/` | Harness, observers and independent oracles (added per lane) |

## Gate

```text
python -X utf8 -B experiments/v3/capability-composition/check_suite.py
```

## Status

- **R-PC** F01–F16 are implemented and **rerun-verified** at `050936e` (GLM-5.3-Flash; see
  `VERIFICATION.md` and `results/`), but a static review found **full specification fidelity is not
  established** (Q01–Q10) — treat them as `partial` (see `REVIEWS.md`). F17/F18 are `deferred`.
- **R-UI** and **R-MOB** are not started. Lane order: R-PC → R-UI → R-MOB, each a vertical slice
  (fixture + independent oracle + detected mutant + gate) before the next.
- Verified on CPython 3.12.10; the suite is stdlib-only pure functions.
