# Recursion

**Development target: Recursion v3.** After reading the control plane, start at
[the current v3 development guide](docs/v3/START-HERE.md). It maps the intact product
specifications, completed bounded work, remaining blockers and task-specific gates.
V1/v2 remain implementation and regression references, not the default development target.

**Who decides:** `CONTROL_PLANE.md` — the authority constitution (adopted 2026-08-06,
D-010). Hermes is the executive control plane; Claude Code is a bounded executor; Lucas is
final authority and merges every review branch himself. Read it before changing how the
loop is driven.

This repository contains the Recursion v3 product: its kernel and adapter contracts,
specifications, experiments, and construction tooling. Recursion is a provider-neutral
agent platform. Other repositories and workstreams are separate and are not embedded here.

## Layout

| Path | Churn | What it is |
|---|---|---|
| `ANCHOR.md` | Yearly | Success criteria, what's explicitly not happening, kill criteria |
| `CONTROL_PLANE.md` | Rare | Authority constitution: Lucas → Hermes → bounded workers and independent verifiers |
| `AGENTS.md` / `CLAUDE.md` | Rare | Operating rules for agent sessions in this repository |
| `docs/v3/START-HERE.md` | Reviewed | Current v3 orientation, specification map, source status and gates |
| `schemas/control-plane/v3/` / `scripts/v3/` | Reviewed | Bounded v3 contracts and planning replay |
| `experiments/v3/` | Reviewed | Scoped experiments and evidence checks |
| `tools/construction_swarm/` | Reviewed | Bounded Devin construction tooling, separate from the v3 product |
| `config/v2/` | Reviewed | Versioned engines, providers, pools, hosts, sandboxes, projects, and routing policies |
| `schemas/control-plane/v2/` | Reviewed | Strict wire contracts for control-plane records |
| `scripts/mvp_*.py` | Reviewed | MVP contracts, packet kernel, adapters, orchestration, and control surface |
| `scripts/execution_fabric.py` | Reviewed | Placement, leases, recovery, verification receipts, and review integration |
| `state/` | Runtime | Canonical goals, tasks, decisions, events, inbox, and generated views |
| `loop/` | Rare | Supervisor scripts, prompts, routing config |
| `docs/` | Reviewed | Architecture, operator guides, plans, and exact-source audits |

## Historical baseline: Recursion v2 source-only MVP

The custom harness is implemented and independently accepted on
`loop/work/recursion-v2-mvp`. It provides:

- deterministic, content-addressed task/budget/capability/evidence contracts;
- immutable depth-2 task packets with bounded context and exact parent authority;
- capability negotiation for Codex CLI, Cursor CLI, Hermes API, OpenCode/OpenRouter,
  and local Qwen;
- host/pool-aware placement, durable leases, replay-safe results, restart recovery,
  authenticated verification receipts, and serialized same-project integration;
- Telegram command semantics for `/today`, `/status`, `/why`, `/next`,
  `/budget`, `/pause`, `/resume`, and `/approve`;
- a source-only second-box controller-death watchdog;
- review-only publication through `scripts/publish.sh`.

For v3 source checks, use the commands in [START-HERE](docs/v3/START-HERE.md).
The v2 gate below does not certify v3 completion.

See `docs/recursion-v2-mvp-harness.md` for the complete capability and trust-boundary
reference. This is source-ready, not production-deployed.

## V2 baseline acceptance and publication

```bash
# Complete source-only acceptance gate
python3 -u scripts/tests/recursion-v2-mvp.test.py

# Review-branch publisher; requires human-owned enablement, registry, and key
bash scripts/publish.sh --branch loop/work/recursion-v2-mvp --repo /opt/recursion
```

The publisher cannot target the default branch and refuses under PAUSE. Lucas reviews and
merges the resulting PR, then separately authorizes credentials, deployment, restart, and
broader autonomy.

## V2 baseline status

Source-only MVP gate passed on 2026-08-31. Review-branch publication and all production
seams remain human-controlled. Exact evidence is in
`docs/audits/recursion-v2-mvp-completion-2026-08-31.md`.
