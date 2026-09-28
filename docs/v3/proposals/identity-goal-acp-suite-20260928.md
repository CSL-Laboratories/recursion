---
title: Recursion v3 identity, /goal, ACP suite and ownership changes
type: proposal
status: owner-review-required
last-verified: 2026-09-28
related: [PRODUCT.md, CLI.md, INTERACTION-SURFACES.md, EXECUTION-FABRIC.md, ROUTE-CAPABILITIES.md, SECURITY.md, CONTEXT.md, STATE.md, EXECUTIVES.md, DECISION-REGISTER.md, RESEARCH-AGENDA.md, RESEARCH-PROCEDURE.md]
---

# Recursion v3 identity, /goal, ACP suite and ownership changes

This proposal is **non-normative until ratified**. It records accepted Owner direction from the
2026-09-28 session and the concrete spec edits that follow. It supersedes the foundry-era framing
of "autonomous looping". It does not itself change any spec file; the per-file edits are listed
below for a reviewed authoring change.

## Owner decisions recorded (2026-09-28)

1. **Recursion owns the canonical prompt and the model token stream in both execution lanes**
   (native and upstream). Adapters normalize/serialize only and never own cursors or credits.
2. **One canonical `RecursionSession` object** that spans turns, route/provider epochs, engine
   switches and detach; provider sessions map N:1 and remain disposable.
3. **`/goal` is in-session Goal mode** (Codex-style), not an autonomous loop.
4. **Cross-session agent chat is bounded consultation plus UI projections** — typed
   answer/decline/blocker with adoption records; never a free-form authority-bearing transcript.
5. **`decision.foundation.authentication` resolves to option 1** (route-specific BYOK +
   user-authenticated upstream binaries; no token interception), and a **companion constitutional
   amendment (CA-V3-007)** is authorized to cover the provider-neutral route model and two auth planes.
6. **Local runtimes are first-class in v1**: llama.cpp, Ollama, and an OpenAI-compatible base URL;
   BYOK and upstream-harness engines also v1; subscriptions roll out per route.

## Identity

Recursion is a self-hosted, provider-neutral **agent operating system and native harness**. It
runs its own goal-driven loop and drives a pluggable suite of endpoints — **local models**
(llama.cpp / Ollama / OpenAI-compatible), **bring-your-own-key APIs**, **subscription/OAuth
routes**, and **upstream harness engines** — behind one durable contract of authority, budget,
context, evidence and recovery. `/goal` is its primary interface; the owner keeps sovereignty and
final say.

It is a **harness at heart** (native loop, OR-022) *and* a **control plane** (drives other
harnesses). It is the **middleman** between the owner and the AI suite so prompt construction and
compaction stay clean and the owner is never locked into one ecosystem.

## Model plane — four endpoint kinds

`EndpointKind` is orthogonal to `BillingBasis` and to `Engine`/`Provider`:

| EndpointKind | What it is | BillingBasis | v1? |
|---|---|---|---|
| `local` | on-host inference via llama.cpp / Ollama / OpenAI-compatible base URL | `local` | yes |
| `byok` | user-supplied provider API key | `metered` | yes |
| `subscription` | provider/OAuth plan entitlement | `subscription` | per-route, incremental |
| `upstream-harness` | a vendor binary performs inference (Codex, Cursor, Claude Code, OpenCode `acp`, Hermes, Command Code GOAT) | varies | yes |

An owner authorizes a set of these as a **route suite** inside the existing
`RoutePolicyEnvelope@1`; `GoalSpec` binds to it; `/goal` routes within it.

## Two auth planes (must stay separate)

- **Plane R — route auth:** how Recursion reaches a model. Provider-native auth homes, BYOK keys,
  or a subscription's sign-in performed **by the upstream binary**. Rule, promoted to normative:
  *Recursion MUST NOT collect, intercept, pool or replay upstream session tokens.* (Some
  subscriptions — e.g. Anthropic — do not permit OAuth into third-party harnesses; the
  vendor-binary own-sign-in is the workaround.)
- **Plane O — Recursion operator auth:** how the owner/API authenticates **into** Recursion
  (installation trust root, operator credential, device roles, step-up). Separate object,
  separate receipts.

## The ownership flip (central)

Today the spec assigns prompt/context construction to the execution layer and stream parsing to
external engines. Change it: **Recursion assembles the canonical prompt** (invariant
prefix + volatile suffix, derived from the `ContextCapsule`) and **emits model token events into
its canonical stream** for both lanes. Adapters only normalize upstream deltas and never own
cursors/credits/coalescing. This is what makes UI/GUI multiplexing, system-prompt control and
cross-session compaction Recursion's to optimize.

## `RecursionSession` + cross-session prompt equivalence

- Add a kernel-minted, project-scoped `SessionId`; lifetime spans turns, route/provider epochs,
  engine switches, detach/resume and compaction transitions; bound to one logical actor + GoalSpec.
- No `SessionStore`: the session is a **projection over the canonical event ledger** plus the
  artifact index; raw transcript stays a retention-class artifact; the capsule is the carrier.
- **One compaction governor per session** (not merely per route epoch).
- Add a **compiled-prompt manifest** (content-addressed) and a per-adapter **compile receipt**;
  the same GoalSpec revision + capsule digest must yield an equivalent control prefix on every
  engine. This is the cross-session/cross-engine cleanliness contract.

## `/goal` (in-session Goal mode)

Owner-authenticated, persistent, steerable, bounded. The goal text is both the first prompt and
the completion criteria (Outcome / Constraints / **Verification**). Progress row supports
pause / resume / edit / clear; live steering via messages; `/plan`-first shaping. Compiles to a
`GoalSpec` and one bounded `ArmRun` (`AUTONOMOUS-PLANNING.md` already permits a direct Owner
command to supply+accept a spec digest). Cancellation maps to the existing **Stop** modes
(Aggressive / Full / Lazy) and immediate PAUSE. It does not grant broader access.

## ACP roles

- **Recursion as ACP client** — drive upstream ACP agents (OpenCode `acp`, Gemini CLI, Cursor,
  future) as engine adapters under one negotiated capability descriptor, so `/goal` can route
  across the suite.
- **Recursion as ACP agent/host (later)** — expose an ACP surface so editors/IDEs can drive
  Recursion (the t3-style control-plane competing surface).
- Reconcile per-route **ACP vs SDK vs CLI** selection; handle ACP init side-effects (isolated
  workspace) and `session/set_mode` (plan/build). ACP never grants authority.

## Cross-session agent chat

Bounded consultation only: request binds parent goal/task, both role leases, decision class,
permitted capsule, response schema, expiry, depth/cycle bound, shared budget; receiver answers /
declines / returns a typed blocker; caller records **adoption** + evidence. UI projections
(side chat, share, project/session views) read over this envelope. `AssistantIdentity` is
promoted from `proposed` to a canonical object.

## Blockers to clear

- `decision.foundation.authentication` → resolve to option 1.
- **CA-V3-007**: the current `CONSTITUTIONAL-AMENDMENT-PROPOSAL.md` covers ownership/PAUSE/verifier
  only; it does **not** cover OR-001's provider-neutral route model or the two auth planes. Add a
  companion amendment (or extend OR-020).

## Per-file change list

| File | Change |
|---|---|
| `PRODUCT.md` | New identity thesis line; promises: harness-and-OS, four endpoint kinds, Recursion owns prompt/stream; `/goal` as primary interface |
| `ARCHITECTURE.md` | Native + upstream both emit into Recursion's canonical stream; adapters normalize only; capability descriptor gains `session-id mapping (N:1)` and `cross-session context handoff` |
| `TERMINAL-RUNTIME.md` | "Canonical model/turn output stream" subsection under the versioned client protocol; bind token deltas to `EVENT/ACK/CREDIT/RESET`; `attach session <id>` |
| `INTERACTION-SURFACES.md` | Canonical stream + multiplexing owned in both modes; declare ACP client (and later ACP agent) roles; `/goal` UX; bounded consultation projections |
| `CONTEXT.md` | `## Canonical prompt assembly ownership`; rescope governor to **per session**; new `## Cross-session prompt-consistency contract` (compiled-prompt manifest + compile receipt) |
| `STATE.md` | Session aggregate over the ledger (no SessionStore); session lifecycle events |
| `GLOSSARY.md` | `RecursionSession` / `SessionId`, `TurnStream` / `StreamEvent` / `ViewSubscription`, `EndpointKind`; `ProviderSession` maps N:1 |
| `M1B-WIRE-PROFILE.md` / `M1B-EXECUTION-CONTRACTS.md` | `EndpointKind` + `route_auth_kind` fields; closed `EVENT` class carries normalized model-token events |
| `EXECUTION-FABRIC.md` | Endpoint-kind as a resolution axis; engine-adapter contract (accept SessionId + capsule; report native session ids as affinity only; typed candidate resume/fork/compaction) |
| `ROUTE-CAPABILITIES.md` | Local runtime adapters (llama.cpp/Ollama/OpenAI-compatible); add Command Code GOAT; add `subscription-eligible-unverified` rollout state |
| `SECURITY.md` | Label Plane R vs Plane O; normative no-interception rule |
| `CLI.md` | `/goal`; endpoint-suite + provider auth/key commands; promote no-interception rule |
| `EXECUTIVES.md` | Promote `AssistantIdentity` + consultation envelope |
| `AUTONOMOUS-PLANNING.md` | `/goal` one-shot GoalSpec supply+accept; widen `allowed_spend_classes` to the four bases (metered still refused without authorization) |
| `DECISION-REGISTER.md` | New OR: Recursion owns canonical prompt/stream + multiplexing in both lanes. New OR: canonical `RecursionSession`. Resolve authentication (option 1). Extend/replace OR-020 for CA-V3-007 |
| `RESEARCH-AGENDA.md` | Add Astra lanes (see `research-lane/PLAN-20260928.md`) |

## Sources

- Codex `/goal` and long-running work: developers.openai.com/codex/reference/slash-commands,
  developers.openai.com/codex/long-running-work
- Agent Client Protocol: agentclientprotocol.com/get-started/introduction,
  agentclientprotocol.com/protocol/overview
- OpenCode ACP: opencode.ai/docs/acp/
- Astra research report (2026-09-28) and the Mark 3 Gemini harness conversation — pinned into
  `SOURCE-REGISTER.md` when the research packets are opened.
