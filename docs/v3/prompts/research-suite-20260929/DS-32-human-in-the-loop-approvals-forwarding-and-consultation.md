---
title: DS-32 — Human-in-the-loop approvals, forwarding, and consultation
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-32 — Human-in-the-loop approvals, forwarding, and consultation

This is an inert Deep Research handoff. It launches nothing, authorizes no provider call, write, spend or configuration change, and is inert until Lucas explicitly invokes this file in a new session. Run it as a public/synthetic research lane and include no private conversation content.

## Purpose

Decide the mechanisms by which a human stays meaningfully in the loop for approvals, steering and consultation, and how those mechanisms behave when work is unattended, remote or resumed. This is blocking because the accepted v3 interaction contract requires the ten terminal controls, three Stop modes, preserved forks, direct Owner-to-ARM conversation and a durable consultation envelope, and because an approval mechanism that merely appears to mediate an effect is a safety failure. The research must map concrete mechanisms onto the boundaries in `docs/v3/INTERACTION-SURFACES.md`, `docs/v3/EXECUTIVES.md` and `docs/v3/TERMINAL-RUNTIME.md` without changing them.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed reconciliation, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. Do not reopen accepted Owner decisions (for example OR-037, OR-038, OR-039) or the accepted interaction boundary, and do not select a topology, transport or protocol as a product decision.

## Inputs to read

- `docs/v3/INTERACTION-SURFACES.md` (accepted boundary)
- `docs/v3/EXECUTIVES.md`
- `docs/v3/TERMINAL-RUNTIME.md`
- `docs/v3/CLI.md`
- `docs/v3/SECURITY.md`
- `docs/v3/CONTEXT.md`
- `docs/v3/PLANNING.md`
- `docs/v3/PRODUCT.md`
- `docs/v3/START-HERE.md`
- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`

## Research questions

### Approval and mediation mechanisms

1. How do approval callbacks, always-invoked hooks and pre-effect mediation differ in where the enforcing resource is, and which failure class does each fail to close when it is not the effecting resource?
2. Under what documented conditions can an approval reply be stale, replayed, re-targeted to a changed action, or answered by a host rather than a human, and how is each detected and rejected?
3. What does a pre-effect mediation boundary need to record — exact action, arguments, scope, current grant, principal, idempotency and receipt — to make "no undeclared effect" falsifiable?
4. Where do upstream harness approval and permission systems remain distinct from Recursion grants and resource containment, and what can be imported reliably?

### Steering, queued input, and question durability

5. How do active-turn steering, queued input and clarifying questions differ in target identity, delivery guarantee and effect on an in-flight turn, and what evidence proves actual delivery rather than acceptance?
6. How are deferred questions and waiters made durable so a crash, reconnect or delayed answer cannot target a replacement question or resurrect a stale one?
7. What is required so a status question does not cancel an assignment, and so a queued follow-up is never confused with a steer into the current turn?
8. How should multiple clients observe one conversation without an acknowledgment of presentation being mistaken for a semantic acknowledgment?

### Terminal controls, Stop modes, and preserved forks

9. How do the ten terminal controls translate to concrete observable contracts and decisive negative traces across local and remote work?
10. What distinguishes Aggressive, Full and Lazy Stop in scope, boundary and required evidence, and where does each still leave an effect ambiguous?
11. How is a preserved fork captured so original unfinished work is untouched, the derived version is separate, and the parent allowance is not duplicated?
12. How are conversation fork, worktree creation and filesystem snapshot kept as three distinct operations, and what binding must survive for recovery?

### Owner-to-ARM conversation and consultation

13. How is a direct Owner-to-ARM instruction represented as an attributable event under an existing GoalSpec without silently creating a goal or widening authority?
14. What does a consultation envelope need to bind (requester, recipient, leases, goal/task version, permitted context, response schema, expiry, depth bound, parent reservation) to keep advice, delivery and adoption as separate lifetimes?
15. How is an authoritative adoption decision kept distinct from a reply so that a late answer can remain attributable evidence while being ineligible under a changed lease?
16. What public multi-agent or assistant systems expose explicit advice-versus-adoption or lease semantics, and what do their documented failure modes show?

### Notification, forwarding, and recovery

17. How should owner-facing notifications be projected to remote surfaces with revoke, rate-limit, coalescing and recovery behavior, without presentation throttling delaying a canonical control event?
18. What recovery applies when a notification transport, bot token or remote view is revoked, replaced or unavailable, and how are duplicates and out-of-order delivery made harmless?

### Comprehension, accessibility, and truthful activity

19. How are comprehension, accessibility (including screen-reader and plain-text modes), input stability and rendering cost measured separately from raw responsiveness?
20. How are truthful activity views defined so engine activity, canonical acceptance, provider reasoning visibility and honest uncertainty are shown without fabricating progress, and which synthetic fixtures — stale steer, in-flight fork, interrupted question, duplicate and out-of-order messages, transport restart, PAUSE and revocation races — prove the mechanisms work?

## Deliverable

- A source-pinned claim table (claim, source, version/date, classification as fact/inference/hypothesis/proposal, confidence, expiry or falsifier trigger).
- A control-by-control and mode-by-mode matrix mapping each mechanism to an observable contract and a decisive negative trace, with unknown cells marked unknown.
- A consultation-envelope proposal that keeps advice, delivery and adoption as separate receipts, and a statement of where advice and authority must not be conflated.
- One recommendation tied to a named competing option and a decisive falsifier, plus open Owner questions.
- An accessible, truthful-view requirement list that does not rely on hidden reasoning.

## Non-goals

- No runtime, transport, terminal, daemon or protocol implementation.
- No language, store, framework, topology or transport selection as a product decision.
- No reopening of OR-037, OR-038, OR-039 or the accepted `INTERACTION-SURFACES.md` boundary.
- No private conversation content, transcripts, local paths or credentials in any public packet.
- No provider, model, Telegram or paid-broadcast calls, and no live trial executed.
- No treating an acknowledgment, a protocol method name or a host-generated reply as human approval.
- No rate-limit bypass, paid-broadcast fallback or policy circumvention.
- No change to PAUSE, spending, deployment, publication or authority state.

## Completion gate

- Every consequential factual claim is pinned to official documentation or a primary source with version and date, or marked unverified.
- The control and Stop matrices cover every listed control and mode with no silent gap.
- Advice, delivery and adoption are represented as separate states with separate settlement.
- Approval mechanisms state where they are not the effecting resource and therefore cannot close the failure class.
- Accessibility and truthfulness requirements are stated as testable outcomes, not claims of achieved quality.
- Every recommendation names a competing option and a decisive falsifier.
- No Owner decision is reopened and no proposal is promoted into an Owner decision.
- A privacy and secret scan of the packet is clean and no protected path is edited.
- Remaining empirical unknowns and Owner questions are listed explicitly.
