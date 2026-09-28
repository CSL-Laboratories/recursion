---
title: Recursion v3 security and sovereignty direction
status: accepted-direction-with-open-threat-model
last-updated: 2026-09-03
---

# Recursion v3 security and sovereignty hypothesis

## Foundation engine-control and credential boundary

The resource performing an effect must enforce the relevant grant/fence; approval callbacks,
event visibility, process kill and signatures are insufficient by themselves. Cover shell
descendants, independent network actions, internal retries, stale packets, revocation and uncertain
receipts. Missing required control means narrower admitted scope or refusal, including for a
native Recursion loop. FND-003/FND-005.

Keep API/BYOK, SDK authentication and native binary login as separate routes. Current Anthropic
guidance permits a conditioned unmodified-binary route with user-owned native authentication while
restricting third-party login/token intermediation. Verify intended distribution and billing before
public support claims. Do not infer universal OAuth permission from Codex technical interfaces.
The [source-bound compatibility record](foundation-research/compatibility.json) is research evidence,
not credential handling code or an entitlement certificate. FND-006.

Claude Code print mode may load repository hooks and MCP configuration without a trust dialog
(SRC-V3-139). Proposed adapters must enumerate and admit that configuration before startup.
Native permission handling (SRC-V3-140/SRC-V3-143) remains distinct from Recursion grants and
resource containment; neither a resumed plan mode nor an inherited session grant authorizes
canonical work. This is an adapter requirement, not an execution performed by this campaign.

Single-authority multi-node recovery is distinct from hosted multi-tenant isolation. Model
offline revocation, split brain and reconnect revalidation before deployment claims. Imported
commands/plugins retain independent grants and cannot inherit authority from source reputation.
See [red designs](foundation-research/evidence.json). FND-013/FND-016.

## Security objective

Protect owner identity, privacy, source, data, credentials, machines, and publication authority without turning open-source software into a service that can lock its owner out. Security controls become mandatory where one identity, process, plugin, or node crosses into another trust domain. Local owners retain the ability to configure and run their own software.

## Owner Sovereignty guarantee

V3 should preserve:

- local and self-hosted operation;
- bring-your-own subscriptions, accounts, APIs, and models;
- inspectable and portable configuration;
- offline-capable operation where configured capabilities permit it;
- custom builds and federated plugin sources;
- owner-selectable permission profiles, including full-access modes;
- no mandatory vendor authorization or central marketplace;
- no telemetry export without explicit opt-in.

Sovereignty does not cause one component to inherit every owner privilege. An owner may choose broad profiles after clear warnings; delegated agents and remote nodes still receive explicit attributable grants so compromise and mistakes can be contained.

## Permission profiles

Candidate profiles include:

- `observe-only`;
- `sandboxed`;
- `workspace-write`;
- `trusted-autonomous`;
- `control-plane-authoring`;
- `absolute-full-access`.

Adapters may map these to provider-native options, including dangerous skip-permission flags when the owner explicitly selects them. V3 must display the effective capabilities and blast radius, record the acknowledgement, and avoid implying that a provider-native flag overrides Recursion's budget, privacy, identity, or publication contracts.

The product must allow an owner to configure genuinely expansive Full Access. A maximum
`absolute-full-access` or `sovereign` preset may include control-plane authoring, selected
credential administration, provider enrollment, metered spending, publication, merge,
deployment and service control when the owner grants those capabilities explicitly. The
product must not advertise Full Access while silently withholding a documented capability.

The implementation direction is a capability vector rather than a single ambient boolean.
Each high-impact capability names its project/host/account scope, limit, acknowledgement,
authentication strength, start/expiry and revocation behavior. A preset may select every
supported capability. When the owner explicitly includes `grant-admin`, a maximum `sovereign`
actor may create, widen, narrow or revoke its own grants within the installation's maximum
authority. Every mutation is a schema-checked kernel action with before/after scope, reason,
authentication, expiry and durable receipt; model text alone cannot change authority.

Grant administration does not automatically include audit-retention or audit-erasure authority.
Historical receipts cannot be silently rewritten or concealed. Any permitted deletion follows
the scoped deletion, backup-expiry and signed-tombstone policy. Full Access changes what the
owner authorizes; it does not make attribution, fencing, receipts or evidence optional.

This product direction conflicts with the current private constitution's stronger human seams
and self-modification prohibitions. It requires explicit reviewed reconciliation before it is
operative; it is not present authority for an agent to change control-plane, auth, routing,
spending, publication or deployment state.

Lower scopes may narrow permissions freely. Widening network, credential, spending, deployment,
or control-plane access requires an explicit owner action at the affected scope unless an
already explicit `grant-admin` capability authorizes the sovereign actor to perform that exact
mutation. Ordinary model, cache, quality, and concurrency preferences may override normally
within inherited bounds.

### Capability grant contract

A grant binds issuer and subject, audience/installation, signing-key identity, parent digest,
delegation depth, GoalSpec and policy versions, exact capability/effect verbs, project/source/
path/branch/host/account/pool/endpoint/privacy scope, resource/spend/rate limits, not-before and
expiry, revocation epoch, lease/fencing token, authentication/step-up receipt and opaque
credential references. Child scope, limits, lifetime and delegation are subsets of the parent.
`grant-admin` is explicit, never implied by an edition or inherited ambiently.

Every grant mutation has one command id and a `GrantMutationReceipt` with exact before/after
digests, authenticated mutator, authorization chain, step-up evidence, policy and admission
epochs, fencing token, reason, result and canonical event sequence. The kernel rejects stale
epochs, replayed nonces, expired/revoked parents, widening outside the installation maximum,
missing before/after receipts and any attempt to bundle trust-root, constitution or
audit-erasure authority into ordinary grant administration.

This contract is schema-ready as a fail-closed requirements shape. Defaults for the installation
maximum, step-up cadence and which audit-retention/erasure mutations sovereign may perform still
require Lucas; no schema may choose those policy answers implicitly.

## Local versus remote boundary

Remote execution requires non-disableable identity, integrity, replay protection, and authorization invariants. These protect connections between nodes; they do not gate local use of the open-source program.

A lightweight, zero-model preflight runs at enrollment or when trust-relevant inputs change. Its result is cached against runtime hash, configuration hash, trust-bundle and protocol versions, certificate state, and relevant permission snapshot. It should complete in seconds on the target class of hardware.

The full conformance suite is mandatory for official v3 release evidence and optional for an individual owner's ordinary installation. A failure quarantines the affected remote capability instead of globally disabling local Recursion.

## Node identity and authorization

- Installation trust root is separate from everyday operator credentials.
- Device enrollment requires proof of possession and explicit owner approval.
- Controller, executor, observer, and verifier privileges are granted separately.
- Parent-goal envelopes and work packets are signed, scoped, expiring, and replay protected.
- Risk-based step-up confirmation applies to authority expansion, high-impact commands, new egress, credentials, updates, and destructive actions.
- Revocation prevents new admission immediately and causes in-flight work to checkpoint or terminate according to the versioned revocation policy.
- The operative current PAUSE is immediate halt. `OR-020` authorizes the proposed `checkpoint`,
  `interrupt`, `drain` and emergency policy family, but it does not become operative merely by
  appearing in a schema or proposal. Ambiguous recovered output is quarantined and cannot
  authorize a new effect.
- A disconnected node may finish only the local bounded operation explicitly permitted by its last valid grant and spool signed evidence. After reconnection, the controller revalidates the GoalSpec version, fencing token, lease, and PAUSE/revocation state before admitting that evidence or any derived action. It cannot widen scope or publish while disconnected.

An owner authenticated on both devices may execute commands remotely when both devices, the goal, and the requested capability are authorized.

The proposed future pause state uses a monotonically advancing admission epoch:

```text
RUNNING(E0) -> QUIESCING(mode,E1) -> PAUSED(mode,E1)
PAUSED(E1)  -> RUNNING(E2) | EMERGENCY_HOLD
```

Every action binds command, principal, GoalSpec/grant/policy digests, admission epoch, fencing
token, effect class, idempotency key and deadline. Precedence is emergency, revocation,
interrupt, then checkpoint. An action admitted before an epoch change but not yet executed is
stale and fails closed. An already accepted external effect becomes ambiguous and is reconciled
by provider identity/status; it is never blindly retried or described as rolled back. These are
future-contract mechanics only: current `CONTROL_PLANE.md` immediate PAUSE remains operative
until the exact [constitutional amendment proposal](CONSTITUTIONAL-AMENDMENT-PROPOSAL.md) is
reviewed and applied.

Verifier assurance uses only the canonical independence vector and downgrade receipt defined in
[`CA-V3-005` and `CA-V3-006`](CONSTITUTIONAL-AMENDMENT-PROPOSAL.md). This document does not define
a second vector. A permitted downgrade records every missing dimension and projects
`downgraded-pass`; it never becomes full independence. V3 constitutional/release work blocks on
an unavailable required verifier unless an authenticated Owner exception has every canonical
binding. These classes remain inert until the amendment is applied.

## Credentials

`recursiond` is hypothesized as the sole credential broker. Secrets remain in provider-native auth homes, an OS secret store, or another configured vault—not Git, SQLite, prompts, worktrees, telemetry, context artifacts, or plugin manifests.

Workers receive:

- an opaque account/credential reference;
- a temporary environment or home overlay exposing only the selected route;
- the minimum lifetime and filesystem visibility required;
- no enumeration of unrelated accounts.

Model-visible output must never contain credential values. Logs and evidence pass a secret scanner before durable storage or publication.

## Project privacy and egress

Every admitted project declares data classification and egress policy. Defaults are `private` and deny-by-default.

A local egress broker validates:

- destination and resolved provider route;
- project classification and provider data-use policy;
- exact GoalSpec and network grant;
- secret-exposure scan;
- artifact classification;
- owner authorization for provider training or contributor-tier routes.

Cheap or contributor routes default to prohibited for private projects unless the owner explicitly enables their data terms. Enabling a contributor route requires a route-specific acknowledgement and an egress broker that captures and classifies the exact outbound bytes before launch; prompt scanning alone is defense in depth, not the boundary. Project configuration and prompt content cannot widen contributor eligibility. Private cache and evidence artifacts remain project scoped; content addressing never implies public or cross-project sharing.

## Provider refusals and safety policy

Refusal handling exists for continuity and compatibility, not for evading provider or legal safeguards. A refusal is recorded and may trigger an explicitly configured alternate route only when the underlying task remains authorized by the GoalSpec, local policy, law, privacy boundary, and provider terms. Repeated or material refusal escalates to the operator.

Recursion may advise that a model/route is a poor fit for an authorized workflow based on observed continuity. The owner may ignore advisory recommendations within valid authority. Hard local boundaries—credentials, privacy, egress, budgets, node identity, and publication—remain enforced independently of model behavior.

## Plugin trust

Plugins declare capabilities, dependencies, event permissions, filesystem/network access, secret references, resource budgets, and isolation needs. Trust modes are:

- `sandboxed`;
- `trusted`;
- owner-authorized `full-access`.

Untrusted plugins never modify the authority kernel, read canonical storage directly, or call
another plugin through hidden global state. A `full-access` plugin still cannot replace kernel
authority checks. Maintainer-approved first-party plugins and releases are signed by the
installation trust root. Unsigned local plugins remain supported and may implement the full
plugin API. On installation, the CLI must state that the plugin is not approved by the
Recursion maintainer team, pin its exact digest and show its requested capabilities.

The owner permits an unsigned-plugin warning/acknowledgement to authorize requested functionality
under a configured local policy. The acknowledgement is digest-specific and must display the
requested authority. Subscription quota consumption, metered spending, provider/account routes,
updates and revocation are distinct effects: installing code must not silently imply unlimited
quota, an unbounded model fleet or metered fallback. Research must compare one-step and staged
consent while preserving a simple novice path. Update signing, provenance and rollback remain
separate from agent execution authority.

## Threats the design must test

- stolen node credential or replayed work packet;
- malicious project configuration widening egress or permissions;
- prompt/tool output attempting to exfiltrate credentials;
- plugin dependency confusion or update compromise;
- symlink/reparse/path traversal across worktrees;
- stale route/GoalSpec used after revocation;
- provider response forging evidence or completion;
- split-brain executive leases;
- disconnected node publishing stale work;
- public event sizes, timing, identifiers, or raw content revealing private project membership;
- owner-hostile defaults that make local recovery impossible.

## Two auth planes - 2026-09-28

Keep two planes distinct and separately receipted:

- **Plane R - route auth.** How Recursion reaches a model: provider-native auth homes, a BYOK key,
  or a subscription's sign-in performed **by the upstream binary**. Normative rule: *Recursion MUST
  NOT collect, intercept, pool or replay upstream session tokens.* Some subscriptions (e.g.
  Anthropic) do not permit OAuth into third-party harnesses; the vendor-binary own-sign-in is the
  workaround. Only an opaque `account_ref` is stored.
- **Plane O - Recursion operator auth.** How the owner/API authenticates into Recursion:
  installation trust root, operator credential, device roles, risk-based step-up.
