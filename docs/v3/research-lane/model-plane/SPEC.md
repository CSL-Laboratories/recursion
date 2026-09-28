---
title: "model-plane research packet"
type: research-packet
status: open
lane: model-plane
priority: P0
last-verified: 2026-09-28
related: [../RESEARCH-PROCEDURE.md, ../METRICS.md, PRODUCT.md, DECISION-REGISTER.md]
---

# Research packet: model-plane

**Question.** Can `local` / `byok` / `subscription` / `upstream-harness` be unified as one owner-authorized route suite with fail-closed eligibility?

**Informs.** OR-042; EXECUTION-FABRIC endpoint-kind axis

**Priority.** P0

**Sources (pin exact pages/commits before executing).** llama.cpp + Ollama server APIs; OpenCode providers; BYOK/OAuth distinctions in ROUTE-CAPABILITIES.

**Method.** Schema the `EndpointKind` + `route_auth_kind`; stub each kind; verify unknown/expired eligibility blocks (never falls back), and that a subscription→metered switch is a billing-authority transition.

**Metrics** (see `../METRICS.md`). Configurability (endpoints supported); price (billing basis correctness); reliability (fail-closed rate).

**Falsifier.** If any unknown/expired route silently falls back, or a metered route is admitted without authorization, this was wrong.

**Gate.** TO BE CREATED: schema check + local-endpoint smoke gate.

**Deliverable / receipt.** Evidence artifact plus `research-attestations/model-plane/<candidate>/` in the
Git common directory.

**Independent verifier.** A different session (different model family where available) reruns the
exact candidate; the implementer's PASS is never acceptance.
