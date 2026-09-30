---
title: DS-28 — Post-M1 construction plugin and route fabric
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-28 — Post-M1 construction plugin and route fabric

This is a bounded construction handoff, not a public Deep Research brief. It is inert until Lucas explicitly invokes it in a new session; creating, reading or listing it launches nothing and authorizes no provider call, write, effect, spend or configuration change. It prepares the exact packet that would build `docs/v3/ROADMAP.md` Milestone 3.

## Purpose

Specify the first bounded construction packet for the plugin kernel and route fabric: manifest-first capability graph, trusted built-in registry, bounded third-party plugin lifecycle, typed event bus, v2 adapter compatibility, route resolver, model-aware capacity pools, billing boundary and credential references. This is blocking because Milestone 3's gate cannot be stated or falsified until the inert contracts M1B froze are projected into a machine-checkable construction packet with a zero-cost disabled path and refuse-before-launch behavior.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. M1B already froze the inert route, plugin, process and broker contracts; do not restart, rebuild or edit it, and treat any required extension as a separately specified bridge. M1A and M1C are likewise merged inert and must not be reopened. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication, push or merge is authorized. Do not select a product language, store, framework or plugin-sandbox implementation. Do not reopen recorded Owner decisions (OR-021 through OR-039).

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/PLUGINS-AND-EDITIONS.md`
- `docs/v3/EXECUTION-FABRIC.md`
- `docs/v3/ROUTE-CAPABILITIES.md`
- `docs/v3/ROADMAP.md`
- `docs/v3/CONSTRUCTION-RUNBOOK.md`
- `docs/v3/M1B-EXECUTION-CONTRACTS.md`
- `docs/v3/M1B-WIRE-PROFILE.md`
- `docs/v3/M1B-FIXTURE-ORACLES.md`
- `docs/v3/construction-swarm/TOOL-PIPELINE.md`
- `docs/v3/prompts/research-suite-20260929/README.md`

## Research questions

### Branch, worktree and task binding

1. What exact reviewed predecessor commit is the `TASK_BASE`, and what isolated worktree, branch name and preflight sequence does the packet freeze before any edit?
2. Which exact new paths may this packet create, and how are all protected paths and the frozen M1B contracts excluded by construction?
3. How does the packet prove it did not modify M1A/M1B/M1C schemas, fixtures, oracles or wire profiles?

### Manifests, registry and lifecycle receipts

4. What manifest fields are required (plugin and protocol version, full artifact digest, transitive dependency-lock digest, provided and required capabilities, exclusive resources, config schema fragment, durable and ephemeral subscriptions, isolation mode, filesystem/network/credential/model access, resource envelope, lifecycle hooks, provenance, license, update source)?
5. How does the trusted built-in registry admit, pin and explain a built-in versus a bounded isolated third-party plugin?
6. Which lifecycle receipts distinguish manifest known, dependency eligible, admitted, loaded/imported, activated, draining, deactivated, failed and unknown cleanup?
7. How is the fenced reload transition expressed so old WorkPackets, leases and subscriptions fail stale and ambiguous effects enter reconciliation?
8. How does the kernel keep manifest resolution and policy checks before any code import or installation?

### Event bus and v2 adapter

9. How are durable lineage events separated from bounded ephemeral operational events, and how is one faulty plugin prevented from blocking the durable writer?
10. How do plugins receive only the event types and fields permitted by their manifest and project classification?
11. What is the v2 adapter compatibility contract, including the four incompatibility responses and the semantic loss map for tools, resources, prompts, hooks, authentication, UI, memory, dependencies and lifecycle?

### Route resolver, capacity pools and billing boundary

12. What independent declarations (task requirements, executive policy, model capability, engine/harness capability, provider and transport, capacity pool and billing basis, credential reference, host/node capability, sandbox and network profile, project privacy and publication policy) resolve into one immutable route?
13. How does the route resolver reject any authority, privacy, billing or capability expansion proposed by a routing plugin?
14. What does a model-aware capacity pool track, and how are opaque flat subscriptions kept as confidence-aware shadow prices rather than fictional dollar prices?
15. How is the billing boundary enforced so switching from a subscription/OAuth route to a metered API route is a billing-authority transition, not ordinary failover?

### Credential references and disabled path

16. How are credential references stored and resolved without storing or logging credentials, and how is credential visibility bounded per route and project?
17. How does the zero-cost disabled path prove no code execution, import or installation, credential access, network call, subprocess, timer, watcher, callback, context/tool enumeration, model turn, quota reservation or spend?

### Refuse-before-provider-launch and denial receipts

18. What refuse-before-provider-launch check runs against the resolved route, and how does a denial receipt record the requested capability, missing requirement, translation loss and effective config?
19. How is the bounded tool-call budget enforced per job, and how does a disallowed tool call consume budget and produce a known completed failure only after model, transport, export and cleanup qualify?

### Independent review and publication boundary

20. What independent review reproduces the exact-candidate fixtures, and how does the packet end at a local candidate commit with no push, publication or merge?

## Deliverable

A machine-checkable construction packet specification: the frozen predecessor commit, worktree and branch; allowed new paths and protected-path exclusions; the manifest, registry and lifecycle-receipt contract; the event-bus and v2-adapter requirements; the route-resolver input and rejection rules; capacity-pool and billing-boundary requirements; credential-reference handling; the zero-cost disabled-path trap; the refuse-before-provider-launch check; the denial-receipt and tool-call-budget contract; the acceptance command inventory; and the independent-review and no-push/no-merge boundary. It is a proposed packet, not an executed result.

## Non-goals

- No push, publication or merge to any branch.
- No restarting, rebuilding or editing M1A/M1B/M1C.
- No provider, model, network or paid call, and no provider launch.
- No plugin installation, import or activation.
- No product language, store, framework or sandbox implementation selection.
- No credential storage, logging or retrieval.
- No billing-authority transition or metered fallback.
- No modification of protected paths or existing specifications.
- No self-review presented as independent verification.

## Completion gate

- The predecessor commit, worktree, branch and allowed paths are frozen and machine-checkable.
- Every required manifest field and lifecycle receipt is explicit and closed.
- The fenced reload transition and stale-lease behavior are stated with expected outcomes.
- Durable and ephemeral event-bus classes are separated, with fault containment for one faulty plugin.
- The v2 adapter preserves the four incompatibility responses and the semantic loss map.
- The route resolver rejects any authority, privacy, billing or capability expansion.
- Capacity pools distinguish billing bases and never invent dollar prices for opaque subscriptions.
- The zero-cost disabled path is a hard-failure test, not a timing tolerance.
- Refuse-before-provider-launch, denial receipts and the bounded tool-call budget are all specified.
- Protected paths and M1 contracts are untouched; independent review is recorded; no push or merge occurs.
