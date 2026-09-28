---
title: "auth-planes research packet"
type: research-packet
status: open
lane: auth-planes
priority: P0
last-verified: 2026-09-28
related: [../RESEARCH-PROCEDURE.md, ../METRICS.md, PRODUCT.md, DECISION-REGISTER.md]
---

# Research packet: auth-planes

**Question.** Can route auth (native sign-in / BYOK / OAuth by the upstream binary, no interception) stay separate from Recursion operator auth, with only opaque account refs stored?

**Informs.** OR-043; SECURITY two-auth-planes section

**Priority.** P0

**Sources (pin exact pages/commits before executing).** Anthropic + OpenAI + Cursor auth docs; ACP `authenticate`.

**Method.** Threat model + receipt fixtures: prove no upstream session token is collected/intercepted/pooled/replayed; separate receipts for Plane R and Plane O.

**Metrics** (see `../METRICS.md`). Reliability (interception refusals); configurability (routes supported); privacy floor.

**Falsifier.** If any path stores, pools or replays an upstream session token, or conflates the two planes, this was wrong.

**Gate.** TO BE CREATED: auth-plane fixture (refusal cases).

**Deliverable / receipt.** Evidence artifact plus `research-attestations/auth-planes/<candidate>/` in the
Git common directory.

**Independent verifier.** A different session (different model family where available) reruns the
exact candidate; the implementer's PASS is never acceptance.
