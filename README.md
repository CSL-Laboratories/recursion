# Recursion

**Recursion v3** is a provider-neutral agent platform: a harness with its own client,
execution kernel, context/compaction engine and multi-node fabric, plus adapters for
supported upstream harnesses. The current development target and single source of truth is
[docs/v3/START-HERE.md](docs/v3/START-HERE.md).

V1/v2 — the previous Hermes-based supervisor and control plane — are **retired**. They were
removed from the trunk on 2026-09-28 and remain recoverable from the
`pre-v3-clean-cut-20260928` tag.

Start here:

- [docs/v3/START-HERE.md](docs/v3/START-HERE.md) — orientation and specification map
- [docs/v3/RESEARCH-SCOPE.md](docs/v3/RESEARCH-SCOPE.md) — research goals and the gates that pass
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute
- [ANCHOR.md](ANCHOR.md) — why the work exists; [CONTROL_PLANE.md](CONTROL_PLANE.md) — who decides

> Governance note: `CONTROL_PLANE.md` and `ANCHOR.md` are the operative authority and scope
> records. They already describe Recursion v3 (D-017, 2026-09-28); the older claim that they
> "still describe the pre-v3 control plane" was stale. Neither file is edited in the
> 2026-10-24 spec amendment (`ANCHOR.md` is a protected path with no founder authoring
> session). GUI-timing supersession for ANCHOR's "terminal first, GUI later" is already
> D-038; additions beyond D-038 live in `docs/v3/AMENDMENT-2026-10-24.md`. Status:
> amendment proposed (2026-10-24); acceptance is a human decision by Lucas.

## Layout

| Path | What it is |
|---|---|
| `docs/v3/` | existing v3 product specifications. New non-public docs go to [csl-research](https://github.com/CSL-Laboratories/csl-research), except operational contracts and runbooks that this repository's code, a gate or CI reads; existing files stay (CSL-536). |
| `experiments/v3/` | runnable product checks only |
| `schemas/control-plane/v3/` | v3 contract schemas (planning, execution, integrity) |
| `scripts/v3/` | v3 planning replay and validation |
| `scripts/v3/harness/` | CSL-27 upstream CLI harness slice; CSL-83 weights, cooldowns, and caps |
| `config/harness.json` | shipped harness config (offline fixtures only) |
| `scripts/tests/v3/` | v3 M1 test programs and fixtures |
| `state/` | decision log and v3 goal/task records |

## Harness slice

CSL-27 adds a minimal upstream CLI harness. It routes one task through an
adapter and can review it with a second adapter. The shipped config enables
only the offline `fixture` and `fixture-b` providers. `claude`, `codex`, and
`grok` start only with `--allow-live`. The architecture note is
[docs/v3/harness/CSL-27.md](docs/v3/harness/CSL-27.md). CSL-83 adds preference
weights, a cooldown after rate-limit, quota, and auth failures, fallback to
the next capable provider, and size caps on review prompts and dependency
context. The note is [docs/v3/harness/CSL-83.md](docs/v3/harness/CSL-83.md).

From the repository root:

```bash
python -m scripts.v3.harness doctor
python -m scripts.v3.harness run --task "Say hello"
```

## Retired

The v1/v2 custom harness — the Hermes executive, supervisor loop, decision queue, project
pilot and reconciler, `work.sh`/`publish.sh`, the MVP contract kernel, execution fabric,
remote adapters, and their configs and deployment units — was removed from the trunk on
2026-09-28 because it contributed no dependency to v3.
Nothing is lost: the complete prior tree is at tag `pre-v3-clean-cut-20260928` and in git
history.
