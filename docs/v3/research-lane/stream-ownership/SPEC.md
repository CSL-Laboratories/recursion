---
title: "stream-ownership research packet"
type: research-packet
status: open
lane: stream-ownership
priority: P0
last-verified: 2026-09-28
related: [../RESEARCH-PROCEDURE.md, ../METRICS.md, PRODUCT.md, DECISION-REGISTER.md]
---

# Research packet: stream-ownership

**Question.** Can Recursion own the canonical prompt + model token stream and multiplex it to N views with no extra model call and no per-token full re-render?

**Informs.** OR-040; ARCHITECTURE/TERMINAL-RUNTIME/CONTEXT ownership sections

**Priority.** P0

**Sources (pin exact pages/commits before executing).** ACP prompt-turn/session-update spec; vLLM prefix-cache docs; llama.cpp server streaming.

**Method.** Replay a captured token stream through the canonical broker; attach 0/1/5/10/20 views; measure append latency, frame/input p99, coalescing, and that view count does not change model calls.

**Metrics** (see `../METRICS.md`). Speed (p99 input/frame); system performance (CPU, wakeups); context/cache (coalesced frames); quality (no lost events under replay/RESET).

**Falsifier.** If N views change the number of model/worker calls, or the p99 frame budget is missed, or a RESET loses canonical events, the design was wrong.

**Gate.** EXISTING baseline: PERF-010 (TERMINAL-RUNTIME); new `experiments/v3/stream-ownership/check.py`.

**Deliverable / receipt.** Evidence artifact plus `research-attestations/stream-ownership/<candidate>/` in the
Git common directory.

**Independent verifier.** A different session (different model family where available) reruns the
exact candidate; the implementer's PASS is never acceptance.
