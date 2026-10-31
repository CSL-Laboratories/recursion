# Recursion

**Recursion v3** is a provider-neutral agent platform: a harness with its own client,
execution kernel, context/compaction engine and multi-node fabric, plus adapters for
supported upstream harnesses. The v3 specifications, research and decision log that used to
live under `docs/` and `state/` moved to csl-research under CSL-536: see
[`contracts/recursion/history/`](https://github.com/CSL-Laboratories/csl-research/tree/f9d8526acbaf02abb227b38d88ed95e170a1f6d4/contracts/recursion/history). The next product contract
will be a `PRODUCT.md` in csl-research.

V1/v2 — the previous Hermes-based supervisor and control plane — are **retired**. They were
removed from the trunk on 2026-09-28 and remain recoverable from the
`pre-v3-clean-cut-20260928` tag.

Start here:

- [csl-research `contracts/recursion/history/`](https://github.com/CSL-Laboratories/csl-research/tree/f9d8526acbaf02abb227b38d88ed95e170a1f6d4/contracts/recursion/history) — the former `docs/` and `state/` records, byte for byte
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute
- [ANCHOR.md](ANCHOR.md) — why the work exists; [CONTROL_PLANE.md](CONTROL_PLANE.md) — who decides

> Governance note: `CONTROL_PLANE.md` and `ANCHOR.md` are the operative authority and scope
> records. They already describe Recursion v3 (D-017, 2026-09-28); the older claim that they
> "still describe the pre-v3 control plane" was stale. Neither file is edited in the
> 2026-10-24 spec amendment (`ANCHOR.md` is a protected path with no founder authoring
> session). GUI-timing supersession for ANCHOR's "terminal first, GUI later" is already
> D-038; additions beyond D-038 live in `docs/v3/AMENDMENT-2026-10-24.md` (now in csl-research
> history, see above). Status:
> amendment proposed (2026-10-24); acceptance is a human decision by Lucas.

## Layout

| Path | What it is |
|---|---|
| `docs/` | only the operational files this repository's code, a gate or CI reads (`docs/ci-event-contract.md`, `docs/v3/x1/`). Every other doc goes to [csl-research](https://github.com/CSL-Laboratories/csl-research) (CSL-536). |
| `experiments/v3/` | runnable product checks only. Earlier runs are kept as evidence in csl-research [`contracts/recursion/evidence/experiments/v3/`](https://github.com/CSL-Laboratories/csl-research/tree/d29bb4a4bec86fb6637eaa7ec3afeaab083eb0e0/contracts/recursion/evidence/experiments/v3) (CSL-596). |
| `schemas/control-plane/v3/` | v3 contract schemas (planning, execution, integrity) |
| `scripts/v3/` | v3 planning replay and validation |
| `scripts/tests/v3/` | v3 M1 test programs and fixtures |
| `state/` | `INBOX.md` (optional escalation) and `PROPOSALS.md` (protected-change proposals). The frozen decision log moved to csl-research history. |

## Retired

The v1/v2 custom harness — the Hermes executive, supervisor loop, decision queue, project
pilot and reconciler, `work.sh`/`publish.sh`, the MVP contract kernel, execution fabric,
remote adapters, and their configs and deployment units — was removed from the trunk on
2026-09-28 because it contributed no dependency to v3.
Nothing is lost: the complete prior tree is at tag `pre-v3-clean-cut-20260928` and in git
history.
