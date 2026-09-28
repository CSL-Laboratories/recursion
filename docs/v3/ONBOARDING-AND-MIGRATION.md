---
title: Recursion v3 onboarding and migration
type: spec
status: accepted-direction
last-verified: 2026-09-28
related: [PRODUCT.md, CLI.md, EXECUTION-FABRIC.md, ROUTE-CAPABILITIES.md, SECURITY.md, research-lane/METRICS.md]
---

# Onboarding and migration

Recursion is meant to be the **central engine for a person's agentic engineering work** without
locking them into one ecosystem. Ease of adoption is a product requirement, not an afterthought:
if switching costs are high, users stay trapped in a single vendor.

## Requirements

- **First-run wizard.** `recursion init` detects the host, existing harness installs (Codex, Cursor,
  Claude Code, Gemini CLI, OpenCode, Hermes, ...), existing credentials/auth homes, and local
  runtimes (llama.cpp / Ollama / OpenAI-compatible), then proposes an owner-authorized route suite.
- **One-command import.** `recursion import <harness>` imports goals/config/instructions where the
  source exposes them; the owner's existing subscriptions and keys are **reused in place** (Plane R)
  and never re-typed or intercepted.
- **No lock-in.** Goals, context, evidence and configuration are portable across engines; migrating
  away is as easy as migrating in. Canonical artifacts (GoalSpec, capsule, evidence) are
  provider-neutral.
- **Continuity.** Imported work keeps lineage; an imported goal becomes a normal GoalSpec/ArmRun.
- **Honest gaps.** An unsupported import path is reported explicitly, never silently dropped.

## Measurable (see research-lane/METRICS.md)

- Time-to-first-successful-`/goal` from a clean machine.
- Fraction of existing subscriptions/keys reused without re-entry.
- Number of supported import paths and their success rate.
- Migration-out fidelity (can a user reconstruct their setup elsewhere).
