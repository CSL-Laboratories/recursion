---
title: "prompt-equivalence research packet"
type: research-packet
status: open
lane: prompt-equivalence
priority: P0
last-verified: 2026-09-28
related: [../RESEARCH-PROCEDURE.md, ../METRICS.md, PRODUCT.md, DECISION-REGISTER.md]
---

# Research packet: prompt-equivalence

**Question.** Does the same GoalSpec revision + capsule digest compile to an equivalent control prefix on every engine?

**Informs.** OR-041; CONTEXT cross-session prompt-consistency contract

**Priority.** P0

**Sources (pin exact pages/commits before executing).** CONTEXT canonical artifacts; provider prompt/cache docs.

**Method.** Build a compiled-prompt manifest + per-adapter compile receipt; diff manifests across adapters and fail on divergence.

**Metrics** (see `../METRICS.md`). Configurability (equivalence rate); context/cache (prefix length, cache hit); quality (behavioral parity on a fixed task).

**Falsifier.** If two adapters produce materially different control prefixes for the same capsule digest without a typed, declared loss, this was wrong.

**Gate.** TO BE CREATED: `experiments/v3/prompt-equivalence/check.py` (manifest diff).

**Deliverable / receipt.** Evidence artifact plus `research-attestations/prompt-equivalence/<candidate>/` in the
Git common directory.

**Independent verifier.** A different session (different model family where available) reruns the
exact candidate; the implementer's PASS is never acceptance.
