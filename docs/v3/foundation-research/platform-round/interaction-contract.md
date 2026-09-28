# Proposed execution and interaction contract

2026-09-06. Product requirements are Owner-accepted; the mechanics below are proposals for
probe/model validation. Reuse [M1A](../../M1A-AUTONOMOUS-PLANNING.md),
[M1B](../../M1B-EXECUTION-CONTRACTS.md), [M1C](../../M1C-INTEGRITY-CONTRACTS.md),
[CLI](../../CLI.md), [terminal runtime](../../TERMINAL-RUNTIME.md) and
[context fidelity](../../CONTEXT.md). No frozen schema changes occur here.

## Ownership across the platform

| Responsibility | Shared Recursion service | Native execution | Upstream execution |
|---|---|---|---|
| Goal/task identity and acceptance | Canonical writer; grants, budgets, exact evidence and verification | Submits typed proposals/results | Adapter translates upstream identities/events; upstream session never replaces task authority |
| Reasoning roles | Durable role/run identity, deterministic dispatch and admission | Supported direct connection executes requested role | Eligible harness session executes requested role, including Head/ARM planning and summarization |
| Model/tool loop | Admits effects and reconciles outcomes | Recursion sampling/context/tool implementation | Upstream loop plus adapter and admitted hooks/tools/plugins |
| Session/context | Canonical control state and referenced evidence; one compaction governor per Recursion session | Consume the canonical prompt assembled by Recursion; engine-side serialization only | Coordinate native session and supported compaction extensions; expose losses |
| Processes and views | Broker lifetime, bounded streams, input ownership and detach/reattach | Managed loop/tools | Managed engine process or supported remote session |
| Configuration | Deterministic resolution, provenance, capability negotiation and effective-time snapshot | Native implementation consumes admitted settings | Adapter/extension compiles settings with unsupported/conflict report |
| Scheduling and accounting | Persistent tasks, timers, reservations, usage observations and reconciliation | Same admission envelope | Same envelope, including upstream internal descendants and unknown counters |

A role binding records logical role/run, task/plan, engine/interface/version, model/provider,
authentication reference, billing pool, host, effective configuration, allowed tools and
verification class. This is a proposed projection of existing Route/WorkPacket concepts,
not a second registry. Every model-using dependency is included: planner, implementer, reviewer,
summarizer, recovery analyst, setup assistant, learned router and model-backed retrieval.
A disabled optional feature makes no calls. Deterministic search/replay may replace inference
only where its actual function satisfies the workflow.

An upstream-only plan must resolve all required role bindings before admission, reserve their
shared allowance and prove that absent API credentials do not trigger fallback. A depleted
eligible pool produces durable waiting or a policy-authorized alternate route. Independent review
can use a clean session on the same engine, but that is not cross-provider independence; when the
required independence class is unavailable, the honest outcome is blocked or an explicitly
recorded permitted downgrade. It is never a fabricated independent pass.

## Ten observable controls

| ID | Feature | Required observable behavior | Candidate ownership and decisive failure test |
|---|---|---|---|
| streaming | Streaming output | Ordered task/turn-tagged output, bounded buffering, durable terminal outcome distinct from transient text | Runtime broker + adapter/native emitter; stalled view cannot block canonical append; gap -> RESET |
| questions | Questions | Correlated durable question, answer/cancel state, expiry and target; answer reaches its intended waiter | Shared interaction store + upstream callback/tool or native loop; reconnect and late answer cannot address a changed question |
| approvals | Approvals | Exact action/arguments/scope/revision shown; valid prior authorization reused; changed action needs revalidation | Runtime admission + resource enforcement + engine hook/callback; reject stale answer and test bypass via tool descendants |
| queued_messages | Queued messages | Accepted queue survives reconnect; edit/remove before delivery; explicit delivery point and disposition | Shared queue + adapter turn delivery; crash between enqueue and acknowledgment cannot duplicate input |
| active_steering | Active steering | Target active turn/attempt and acknowledge delivery; distinguish delivery from semantic compliance | Native turn API or supported upstream control/extension; a next-turn queue must not be labeled active steering |
| interruption | Interruption | Show requested, acknowledged, settled and uncertain in-flight outcomes; stop future dispatch | Shared admission revocation + broker/adapter cancel; kill/ack is not evidence that an external effect was undone |
| resume | Resume | Restore canonical checkpoint and revalidate permissions/config/route; use engine session affinity when eligible | Runtime recovery + native capsule or upstream resume; missing upstream session still permits inspection and explicit rehydration |
| forks | Forks | Explicit lineage, isolated writable artifact/workspace scope, separately reserved share of parent budget | Shared task fork + engine conversation fork/rehydration; two forks cannot each inherit the whole spend allowance |
| compaction | Compaction controls | Inspect/request/tune supported compaction; preserve authoritative constraints, pending interactions and evidence references | Shared governor + native compactor or upstream extension; reject lossy summary and recover from retained evidence |
| commands_plugins | Custom commands/plugins | Namespaced commands, schema/config provenance, supplying component and capability versions visible | Shared command registry + native implementation or supported upstream plugin/MCP/hook; reject collisions and grant expansion |

All ten are product requirements. Per-route absence is visible before admission. Investigate
richer interfaces and supported extensions first, then report faithful composition, explicit
reduced behavior, alternate eligible route or a blocked workflow. A reduced route does not prove
the complete launch requirement. Engine-specific extensions retain their own namespace/version
and do not have to be flattened into the common denominator.

## Configuration composition and lifecycle

The existing CLI order covers defaults, edition, authority domain, project, parent goal, task
and ephemeral override. Account pools, reusable agent profiles and workflow templates need
explicit relations to that order. Proposed approach: scopes provide defaults and preferences;
resource authorities provide intersecting ceilings; profiles/workflows are versioned templates
compiled into an effective snapshot. They do not create new funds or principals.

Resolve each field by its declared merge operator: override, set intersection, keyed merge or
explicit conflict. Avoid one global "most specific wins" rule for all capability and budget fields.
Show value, origin, component, conflicts, authority ceiling and application time. Parent/child
caps are nested reservations against shared pools; observations from other applications are
uncertain inputs, not precisely enforceable global consumption.

One plugin-facing schema can have multiple engine implementations. Its manifest declares
capability/version requirements, settings translation, loss, grants, resource use and test suite.
An engine switch runs a configuration rehearsal before admission and invalidates incompatible
compiled settings. Auto-installing an upstream package is an execution effect, not mere discovery.

Setup/import/repair all use the same proposed-change transaction: detect -> explain -> preview
before/after and conflicts -> validate -> authorize within existing scope -> activate at declared
lifecycle -> verify -> retain recovery path. First-auth bootstrap and offline doctor remain
model-free; the optional assistant uses an explicit eligible role binding after authentication.

Updates bind old/new release, schema, plugins, configuration and active-task policy. Separate
code rollback from durable-state restoration. Rehearse migration and unsupported downgrades;
a safe-mode path must still show canonical evidence when optional extensions are broken.

## Remote continuation and model obligations

Keep client, coordinator, worker, engine, inference provider and account separate. Remote commands
distinguish locally saved, coordinator-accepted and worker-applied. A disappearing client does not
end a task. A disappearing worker does not prove it stopped. Store recovery-critical records away
from that worker; transfer versioned manifests/artifacts and check environment compatibility.

Before replacement, resolve/fence previous ownership at the resource that can perform effects,
preserve ambiguous outcomes, then issue a related attempt on an eligible host. Restore pending
questions, queued inputs, grants, pool reservations, artifacts and verification evidence together.
Cross-OS recovery resumes recorded work; it does not promise migration of a live process image.

Retain the six existing formal-model obligations: M1A/M1C identity projection; distinct epoch
domains; canonical writer exclusion; current-immediate PAUSE; partition/receipt ambiguity; and
bounded flow with RESET before replay after gaps. Configuration digests and role/route bindings
must survive that projection. Small interface probes should falsify these assumptions before
freeze. OR-015's legacy common-writer migration remains blocked. Nothing here changes current
Hermes authority or activates the proposed future PAUSE family.
