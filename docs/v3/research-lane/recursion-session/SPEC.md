---
title: "recursion-session research packet"
type: research-packet
status: open
lane: recursion-session
priority: P0
last-verified: 2026-09-28
related: [../RESEARCH-PROCEDURE.md, ../METRICS.md, PRODUCT.md, DECISION-REGISTER.md]
---

# Research packet: recursion-session

**Question.** Does one canonical `RecursionSession` spanning route/provider epochs preserve directive/decision/checkpoint/accounting continuity across an engine switch, with provider sessions strictly disposable?

**Informs.** OR-041; STATE session aggregate; EXECUTION-FABRIC adapter mapping

**Priority.** P0

**Sources (pin exact pages/commits before executing).** ACP session/new|load semantics; current CONTEXT/STATE design.

**Method.** Fault fixture: native→upstream and upstream→upstream engine switch mid-session; assert directive/decision/checkpoint continuity and that no provider-session id becomes canonical identity.

**Metrics** (see `../METRICS.md`). Reliability (switch success, resume/replay success); quality (no lost decisions); context (rehydration completeness).

**Falsifier.** If a mid-session engine switch loses a directive, decision, checkpoint or budget state, or a provider session id becomes identity, this was wrong.

**Gate.** TO BE CREATED: `experiments/v3/recursion-session/check.py` (session-continuity fixture).

**Deliverable / receipt.** Evidence artifact plus `research-attestations/recursion-session/<candidate>/` in the
Git common directory.

**Independent verifier.** A different session (different model family where available) reruns the
exact candidate; the implementer's PASS is never acceptance.
