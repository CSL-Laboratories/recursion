---
title: DS-12 — Context compaction and cache economics
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-12 — Context compaction and cache economics

Run this as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Decide which lawful, documented cache, session and context controls reduce repeated work without hidden coupling, privacy leakage or fidelity loss, and whether provider prompt caches and Recursion-owned evidence reuse can be kept honestly separate. This blocks the context governor, the execution-context capsule policy and the evidence-cache design. It is blocking because no local experiment yet measures compaction loss, priming cost, invalidations or cross-route continuation.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This prompt must not change provider, model or authentication configuration. "Cache manipulation" here means only supported prompt, layout, session and provider cache controls plus local evidence reuse; it never means bypassing provider policy or fabricating usage.

## Inputs to read

- `docs/v3/RESEARCH-AGENDA.md` (lane R14)
- `docs/v3/CONTEXT.md`
- `docs/v3/EXECUTION-FABRIC.md`
- `docs/v3/ROUTE-CAPABILITIES.md`
- `docs/v3/TELEMETRY.md`
- `docs/v3/foundation-research/mark2-research/mark3-comparison/architectures.md`
- `docs/v3/foundation-research/synthesis.md`

## Research questions

1. Which cache, session and context controls does each supported route actually expose at a dated version (endpoint, model class, explicit versus implicit, breakpoints, lifetime, retention), and which fields are documented, disabled or unknown?
2. Which cached-read, cache-write and billing semantics are documented per endpoint, and which cannot be transferred to an OAuth or harness route without route evidence?
3. Does a deterministic stable prefix plus content-addressed repository evidence reduce repeated input on same-route continuation when measured against a cold baseline that includes priming cost?
4. Does cross-route continuation reuse any provider cache, or only Recursion-owned semantic artifacts, and what quality loss appears after session reconstruction?
5. Which invalidation events (source change, configuration change, revoked instruction, changed tool or resource result) must force rehydration, and how accurately does a repository map invalidate by commit and configuration digest?
6. Does repeated compaction lose owner directives, gates, blockers or evidence references, and which deterministic checks detect each loss?
7. Can one compaction governor per route epoch be enforced, with native and session compactors returning candidate artifacts only and unable to advance the authoritative epoch?
8. Does session affinity beat fresh-session capsule reconstruction once priming, refresh and recovery cost are included?
9. What privacy leakage arises from cache keys, digests, size or timing patterns, or cross-project deduplication, and how is private-project isolation verified?
10. How complete is cold recovery from the non-droppable capsule plus content-addressed artifacts when no provider session survives?
11. How many cache writes and reads, and how many compactor, refresh, retry and output calls, occur per workflow, and how are unavailable counters kept unknown?
12. What is the total-cost counterfactual for keepalive or refresh, and does it ever repay priming under a no-future-work edge case?
13. Which cache-hit or savings claims are unsupported at current dates and must carry an expiry trigger?
14. What is the correct unit of account when provider cache, application evidence reuse and subscription debit are three different things?

## Deliverable

- A dated route-exposure table that separates provider cache controls from Recursion-owned evidence and context reuse, never conflating the two.
- A source-pinned claim table with classification, confidence and expiry, including explicit "unknown" rows.
- Options with tradeoffs for stable-prefix layout, session affinity, capsule hydration, evidence caching and compaction policy.
- A recommendation tied to a competing option and a decisive falsifier, plus open Owner questions.
- Explicit preservation of the non-droppable capsule and the one-compaction-governor-per-route-epoch requirement.

## Non-goals

- Bypassing provider policy or fabricating cache usage, cache hits or savings.
- Inferring a cache hit or subscription debit from a documented flag or a single observation.
- Adopting a paid explicit cache treatment, a default lifetime, or periodic model keepalive.
- Dropping the non-droppable capsule or allowing more than one compaction governor per route epoch.
- Sending private project content or raw content digests to a public route.
- Changing provider, auth or configuration, or running any cache experiment against a paid route.
- Reopening accepted Owner decisions or promoting a cache policy into a default.

## Completion gate

- Every provider exposure is pinned by endpoint, model class, source and retrieval date.
- Provider cache and Recursion-owned evidence reuse stay separate in every claim.
- Every recommendation names a competing option and a falsifier.
- No cache hit or saving is asserted without dated route evidence; unsupported claims carry expiry.
- The capsule and one-governor-per-epoch requirements are preserved explicitly.
- Unavailable counters stay unknown; no usage is fabricated.
- The privacy and secret scan is clean and no protected path is edited.
- Open Owner questions are listed and no decision is promoted.
