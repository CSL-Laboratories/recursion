---
title: "acp-suite research packet"
type: research-packet
status: open
lane: acp-suite
priority: P0
last-verified: 2026-09-28
related: [../RESEARCH-PROCEDURE.md, ../METRICS.md, PRODUCT.md, DECISION-REGISTER.md]
---

# Research packet: acp-suite

**Question.** Can one ACP client drive a suite of upstream agents as engines while Recursion keeps session/authority, and can Recursion later expose itself as an ACP agent?

**Informs.** OR-045; INTERACTION-SURFACES ACP roles

**Priority.** P0

**Sources (pin exact pages/commits before executing).** agentclientprotocol.com protocol pages; OpenCode `opencode acp`.

**Method.** Fake ACP transport fixture: initialize/session/new/prompt/set_mode/cancel, capability negotiation, unsupported-invariant refusal; assert Recursion keeps SessionId/authority.

**Metrics** (see `../METRICS.md`). Configurability (harnesses driven); reliability (fail-closed on unsupported); context (session mapping).

**Falsifier.** If an adapter's session id becomes Recursion identity, or authority is inferred from ACP, this was wrong.

**Gate.** TO BE CREATED: `experiments/v3/acp-suite/check.py` (fake transport).

**Deliverable / receipt.** Evidence artifact plus `research-attestations/acp-suite/<candidate>/` in the
Git common directory.

**Independent verifier.** A different session (different model family where available) reruns the
exact candidate; the implementer's PASS is never acceptance.
