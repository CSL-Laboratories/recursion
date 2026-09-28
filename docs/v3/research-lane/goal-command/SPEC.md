---
title: "goal-command research packet"
type: research-packet
status: open
lane: goal-command
priority: P0
last-verified: 2026-09-28
related: [../RESEARCH-PROCEDURE.md, ../METRICS.md, PRODUCT.md, DECISION-REGISTER.md]
---

# Research packet: goal-command

**Question.** Does in-session `/goal` compile to exactly one bounded GoalSpec/ArmRun with Stop-mode cancellation, pause/resume/edit/clear, and no broader access?

**Informs.** OR-044; CLI /goal; AUTONOMOUS-PLANNING direct-owner supply+accept

**Priority.** P0

**Sources (pin exact pages/commits before executing).** Codex `/goal` + `/plan` docs.

**Method.** Inert replay: owner-authenticated `/goal` → GoalSpec digest accept → one ArmRun; assert cancellation maps to Aggressive/Full/Lazy Stop + immediate PAUSE, and permissions do not widen.

**Metrics** (see `../METRICS.md`). Reliability (cancel correctness); configurability; quality (bounded terminal conditions).

**Falsifier.** If `/goal` admits more than one ArmRun, widens permissions, or cannot be stopped, this was wrong.

**Gate.** EXISTING: M1A planning replay gates (`schemas/control-plane/v3/planning`, `scripts/tests/v3/planning/*.test.py`).

**Deliverable / receipt.** Evidence artifact plus `research-attestations/goal-command/<candidate>/` in the
Git common directory.

**Independent verifier.** A different session (different model family where available) reruns the
exact candidate; the implementer's PASS is never acceptance.
