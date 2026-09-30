---
title: DS-15 — ARM conversations and multi-agent rooms
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-15 — ARM conversations and multi-agent rooms

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Decide how `#general`, dedicated ARM topics and shared multi-ARM rooms map onto durable actors without introducing an AI ARM Manager. This blocks the accepted v3 interaction contract because topology, addressing and lease semantics determine whether direct Owner-to-ARM conversation ships without creating a hidden executive tier.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This is lane R2 public/synthetic research; `docs/v3/INTERACTION-SURFACES.md` is the accepted product boundary and must not be reopened.

## Inputs to read

- `docs/v3/INTERACTION-SURFACES.md` (accepted boundary)
- `docs/v3/RESEARCH-AGENDA.md` (lane R2)
- `docs/v3/EXECUTIVES.md`
- `docs/v3/PLANNING.md`
- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `state/portfolio.tsv`

## Research questions

1. Identify the exact SpaceXAI Grok Bot documentation and repository with pinned retrieval dates before any pattern is cited; what does published behavior actually establish?
2. Which official Grok Bot interaction patterns remain usable after rejecting shared ambient credentials, transcript-order authority and a distinct Chief-of-Staff tier?
3. How do durable actors (Head, ARM, worker, verifier, persistent assistant) map onto `#general`, per-ARM topics and shared rooms without adding an AI ARM Manager?
4. Does one bot identity with logical-actor routing, one bot per ARM, or a hybrid give better isolation, credential scope, usability and operational resilience?
5. What are Telegram's current primary-API constraints (forum `message_thread_id`, update retention, update-id reset, polling-versus-webhook exclusivity, per-chat and per-group rate limits), and which constrain the design?
6. How is a command addressed to a logical actor, and how is addressing kept separate from authority so no message widens a GoalSpec?
7. How do shared rooms resolve simultaneous commands, disagreement, cycles and cross-goal budget ownership without transcript order becoming authority?
8. How does the accepted bounded coordination lease (room, workflow step, holder, verbs, budget slice, expiry, fencing token) interact with Head's retained ownership of goal, room membership, authority, conflict, lifecycle and strategic escalation?
9. Which events must Head observe immediately versus summarize into an owner digest, and how is digesting derived from canonical state rather than transcript scraping?
10. How are duplicate, out-of-order and edited/deleted messages normalized, and what recovery applies when a bot token is revoked or replaced?
11. Which public multi-agent orchestration systems expose explicit actor, message, lease and authority models, and what do their documented failure modes show?
12. Does direct Owner-to-ARM conversation measurably reduce owner coordination effort without increasing lost context, contradictory instructions or unreviewed work?
13. What minimum synthetic fixtures prove no chat topology creates a second portfolio executive?

## Deliverable

A source-pinned claim table with classification (fact, inference, unverified), confidence and expiry trigger; options with explicit tradeoffs for each topology; one recommendation tied to a named competing option and a decisive falsifier; and open Owner questions.

## Non-goals

- Do not introduce or recommend an AI ARM Manager or a new executive tier.
- Do not reopen accepted Owner decisions or the `INTERACTION-SURFACES.md` boundary.
- Do not select a default topology, transport or protocol as a product decision.
- Do not request or include private project content, transcripts, credentials or local paths.
- Do not propose runtime implementation, provider/auth changes, spending or deployment.
- Do not treat a route listing, model response or protocol method name as evidence of support.
- Do not design a rate-limit bypass, paid-broadcast fallback or Telegram policy circumvention.

## Completion gate

- The Grok Bot reference is identified with a pinned URL and retrieval date before use, or it is marked unverified and dropped.
- Every consequential factual claim carries a direct primary source or is explicitly marked unverified.
- Every recommendation names a competing option and a decisive falsifier.
- No Owner decision is reopened and no proposal is promoted into an Owner decision.
- Protected paths are untouched; the output is a report, not an edit.
- A privacy/secret scan of the packet is clean.
- Telegram limits are stated as dated observations, not guarantees.
- The report states which questions remain empirically open and which require Lucas.
