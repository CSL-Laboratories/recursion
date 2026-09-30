---
title: DS-29 — Construction fleet separation and continuous operation
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-29 — Construction fleet separation and continuous operation

This is a bounded construction handoff, not a public Deep Research brief. It is inert and proposed until Lucas explicitly invokes it in a new session; creating, reading or listing it launches nothing and authorizes no provider call, write, effect, spend, service, schedule or configuration change. It prepares the exact packet that would build the continuous WSL construction service defined in `docs/v3/construction-swarm/CONTINUOUS-OPERATION.md`.

## Purpose

Specify the first bounded construction packet for separating the construction fleet from the product runtime and for the continuous-operation requirements CW-01 through CW-18, covering implementation phases P1 and P3 through P8. This is blocking because the construction facility must never be hard-coded into the v3 release, and because no 24/7 availability, quota rollover, fenced execution or acceptance claim may be made until each requirement has a named runtime scenario and a machine-checkable gate.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. This packet is proposed and inert: it does not enable a service, route, spend, cron entry or standing autonomy. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication, push or merge is authorized. Do not select a product language, store or framework. Do not reopen completed M1 work or recorded Owner decisions (OR-021 through OR-039). P2 remains the assigned packet in `docs/v3/construction/next-work-20260914/01-sol-service-access.md`, and this packet must not duplicate the product-frontier packet `docs/v3/construction/next-work-20260914/02-astra-product-frontier.md` or the recovery-ledger packet `docs/v3/construction/next-work-20260914/03-sol-recovery-ledger.md`.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/construction-swarm/CONTINUOUS-OPERATION.md`
- `docs/v3/construction-swarm/CAMPAIGN-SUPERVISION.md`
- `docs/v3/construction-swarm/TOOL-PIPELINE.md`
- `docs/v3/ROADMAP.md`
- `docs/v3/EXECUTION-FABRIC.md`
- `docs/v3/PLUGINS-AND-EDITIONS.md`
- `docs/v3/construction/next-work-20260914/README.md`
- `docs/v3/construction/next-work-20260914/01-sol-service-access.md`
- `docs/v3/prompts/research-suite-20260929/README.md`

## Research questions

### Fleet separation and product boundary

1. How is the construction tooling separated from the product runtime so the release exposes only generic engines, nodes and observers, and what artifact proves the construction campaign is not hard-coded into the product?

### Continuous-operation requirements CW-01 through CW-18

2. For CW-01 and CW-02, how do one task authority and a durable outbox couple execution transitions back to canonical tasks, and how does durable fenced execution use leader election, a monotonically increasing incarnation token and unknown-execution retention at every effect boundary?
3. For CW-03, CW-04 and CW-05, how are host lifetime, watchdog, bounded downtime, the shared client protocol (CLI, MCP, remote tunnel), authenticated collaboration and idempotency specified so closing a client cannot terminate the scheduler and a stale revision returns conflict?
4. For CW-06 and CW-07, how do central child admission, the allowed-tool subset, immutable batches versus rolling admission, and one attempt worktree per worker prevent double claims and uncontained desktop results?
5. For CW-08 and CW-09, how are executable task packets, lifecycle states, the persistent evidence dossier, research depth and suspension of non-novel lanes specified without a timer that kills an admitted experiment?
6. For CW-10, how does independent acceptance bind actual tool calls, command exits, hashes, coverage, failures and cleanup, and how does same-family or missing-verifier work block rather than pass?
7. For CW-11, how are adaptive capacity, worker slots, reserved verifier capacity and host headroom admitted as the minimum of host, provider, task-independence, command, verification and Owner ceilings?
8. For CW-12 and CW-13, how do event-driven supervision and deterministic checks batch advisor requests, and how do the standing envelope, quota-window rollover and per-provider account records work without paid fallback or reset-credit redemption?
9. For CW-14 and CW-15, how are bounded recovery, backoff, circuit breakers and unknown retention combined with context-continuity checkpoints that preserve source, authority, budget, gate and unresolved evidence?
10. For CW-16 and CW-17, how are serialized integration, review publication, a durable publication key, reachable steering, honest status and Stop/PAUSE behavior specified so Lucas alone merges and PAUSE never becomes a finish-the-task mode?
11. For CW-18, how is the five-slot versus eight-slot acceleration comparison preregistered with matched tasks, quality rubric, error bounds and uncertainty before any concurrency increase?

### Tool broker, sandbox and job schema

12. How do the tool broker and allowlist prevent ambient host shells, undeclared MCP servers, recursive delegation and writable credential mounts?
13. What command sandbox bounds apply (four machine-wide slots, 60-second commands, 2 GiB per slot, 64 PIDs, two CPU cores, 1 MiB output cap, 64 MiB scratch), and how are descendant cleanup and output-limit responses recorded?
14. How does `write_file` persist only exact allowed paths, require the previous content digest and enforce the 30-changed-file cap?
15. How does the job schema `allowed_tools` refuse during preparation or initialization before provider launch when the list is absent, and how does the broker write a denial receipt for any disallowed tool call?
16. How does Grok advisory supervision return evidence-bound continue, inspect or stop-for-review advice with no authority to accept research, modify workers, renew policy, launch descendants or publish?
17. How do 20-worker batches charge exactly once through the immutable charged attempt directory and use the separate supervision lock to observe running workers?

### Canary, soak and acceptance matrix

18. What one-worker then five-worker canary and 72-hour soak gates must pass on the actual host, and what fresh isolation, identity, receipt, resource and cleanup evidence is required?
19. What acceptance matrix maps every CW requirement to specific runtime scenarios and an implementation phase, and how is a requirement kept unimplemented or unrun rather than marked complete by a demo?
20. How does this packet keep P2 as the assigned `docs/v3/construction/next-work-20260914/01-sol-service-access.md` packet, cover only P1 and P3 through P8, and avoid duplicating packets 02 and 03?

## Deliverable

A machine-checkable construction packet specification: the frozen predecessor commit, worktree and branch; allowed new paths and protected-path exclusions; the fleet-separation boundary; a CW-01 through CW-18 requirement-to-scenario map; the P1 and P3 through P8 phase plan with exit conditions; the tool-broker and allowlist policy; the command-sandbox and `write_file` bounds; the job-schema `allowed_tools` refusal rule; the denial-receipt and bounded tool-call budget; the Grok advisory-supervision boundary; the 20-worker batch charging and supervision-lock behavior; the canary and soak gates; the acceptance matrix; and the independent-review and no-push/no-merge boundary. It is a proposed packet, not an executed service.

## Non-goals

- No enabling of a service, schedule, route, spend or standing autonomy.
- No push, publication or merge to any branch.
- No provider, model, network or paid call.
- No host lifecycle, startup, sign-out or reboot change.
- No PAUSE, auth, credential or configuration change.
- No product language, store or framework selection.
- No duplication of the P2 service packet or the product-frontier and recovery-ledger packets.
- No 24/7 availability or concurrency claim from a stdio or synthetic demo.
- No modification of protected paths or existing specifications.
- No self-review presented as independent verification.

## Completion gate

- The predecessor commit, worktree, branch and allowed paths are frozen and machine-checkable.
- The construction fleet is separated from the product runtime with a stated proof.
- Every CW-01 through CW-18 requirement maps to at least one runtime scenario and one phase.
- P2 is left to its assigned packet, and this packet covers only P1 and P3 through P8.
- The tool broker, allowlist and command-sandbox bounds are explicit and closed.
- `write_file` exact-path, previous-digest and 30-file cap behavior is specified.
- Job-schema `allowed_tools` refuses before provider launch, and denial receipts consume a bounded budget.
- Grok advisory supervision is stated as advisory with no authority.
- The canary and 72-hour soak gates and the acceptance matrix are specified.
- Protected paths are untouched, independent review is recorded, and no push or merge occurs.
