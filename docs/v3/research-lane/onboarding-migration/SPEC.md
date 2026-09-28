---
title: "onboarding-migration research packet"
type: research-packet
status: open
lane: onboarding-migration
priority: P0
last-verified: 2026-09-28
related: [../RESEARCH-PROCEDURE.md, ../METRICS.md, PRODUCT.md, DECISION-REGISTER.md]
---

# Research packet: onboarding-migration

**Question.** What is the time-to-first-successful-`/goal` and the migration cost (in and out) from each supported harness, and can existing subscriptions/keys be reused without re-entry or interception?

**Informs.** OR-047 and ONBOARDING-AND-MIGRATION.md

**Priority.** P0

**Sources (pin exact pages/commits before executing).** Codex `/goal` + long-running docs; OpenCode `/connect` + config; Cursor / Claude Code / Gemini CLI install+auth docs. Pin exact pages before executing.

**Method.** Build a fixture that starts from a clean machine image, runs `recursion init`, and imports from each harness; measure time-to-first-goal, reuse fraction, import success; also test migration OUT (export config + evidence and reconstruct elsewhere).

**Metrics** (see `../METRICS.md`). Onboarding: time-to-first-goal; reuse fraction; import success rate; migration-out fidelity. Plus price (tokens/dollars for the first goal).

**Falsifier.** If a supported import path requires re-entering an existing subscription/key, or time-to-first-goal exceeds a defined budget, this design was wrong.

**Gate.** TO BE CREATED: `experiments/v3/onboarding-migration/check.py` (fixture import + receipt); provider-free.

**Deliverable / receipt.** Evidence artifact plus `research-attestations/onboarding-migration/<candidate>/` in the
Git common directory.

**Independent verifier.** A different session (different model family where available) reruns the
exact candidate; the implementer's PASS is never acceptance.
