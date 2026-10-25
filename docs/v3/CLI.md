---
title: Recursion v3 CLI direction
status: amendment proposed (2026-10-24); acceptance is a human decision by Lucas
last-updated: 2026-10-25
prior-status: accepted-surface-with-open-command-research
---

# Recursion v3 CLI hypothesis

## Mark 3 comparison: revision delivery and revocation - 2026-09-07

A proposed `EffectiveConfigRevision` envelope contains effective digest, preference revision,
authority epoch, changed field classes, application boundary, propagation target and causation.
The unprivileged compiler produces it; the kernel validates identity/scope/grants and current
admission. Both recorded propagation modes remain available. These are probe/schema proposals,
not modifications to immutable M1 objects. CMP-010.

Ordinary preference notifications may coalesce. Revocation and tighter identity, credential,
egress, privacy or resource rules advance a separate monotonic authority fence that cannot be
coalesced away. Every new dispatch rechecks it, even if an active session has not reached its
preference-application boundary. In-flight effects retain their original epoch and report
settled/unknown outcomes; revocation does not pretend rollback. Test R1 preference, R2 revocation,
R3 preference with only R3 notification delivered. Stale R1 authority must still be rejected.

The terminal shows requested versus effective revision, provenance, affected sessions and
application/cleanup failures. A resumed provider session that requires roots/tools/configuration
to be resent cannot silently retain stale values. Numeric epoch representation, notification
encoding and default application boundaries remain for the model and interface probes.

Track compiled, validated, staged and effective revisions per recipient. Envoy xDS supplies a
counterexample to treating ACK as effective application: individual resources can be accepted
without successful overall activation, and a NACK can coexist with partial acceptance. A
connection nonce is not a durable revision. An activation barrier may protect dependencies but
cannot delay the authority fence. Recheck disclosure/hydration against recipient, purpose,
artifact digest and a current policy freshness floor. Cached bytes and propagated trace baggage
do not grant authority. These are adaptation requirements, not a selected control service.
CMP-022.


## Mark 3 configuration and working relationship — 2026-09-07

OR-038/039 require configurable rich collaboration and lean operation. Show capability
availability, task activation, selected default and kernel/service/plugin placement as separate
facts. Existing preferences and natural-language intent can specify the working relationship;
do not require extra forms or repetitive approvals for routine authorized decisions.

A user can inspect effective settings, enter an assistant/Head conversation, steer ongoing
work and issue the selected Stop mode through persistent views. A status question need not
cancel a task. A request changing accepted outcomes, authority, privacy or resource ceilings
still follows Owner amendment rules. Configurable notification and activation mechanics remain
research choices, evaluated for comprehension and full workflow cost.

## Effective configuration revisions — proposed 2026-09-07

MKR-013 retains the existing hierarchy while making merge operators explicit. Preferences use
keyed override; hard authority, egress and budget ceilings intersect; plugin sets merge by stable
ID with conflict rejection; credentials require explicit authority and cannot be selected by a
generic most-specific-wins rule. Profiles and workflow templates compile into a revision and do
not introduce funds or principals. Each field displays requested value, effective value,
source scope/template, merge operator, reason, application boundary and blocked status.

An edit binds expected old digest, proposed new digest, principal/grant, canonical sequence,
field lifecycle and the chosen propagation mode. Persist an application receipt per session.
Authority revocation fences new effects immediately; it does not pretend that an already-started
effect switched grants or stopped. Other changes apply at declared live/checkpoint/restart
boundaries. This resolves the active-session ambiguity without mutating in-flight credentials.

| Session state | Update outstanding sessions | New sessions only |
|---|---|---|
| Active | Mark pending; safe live fields apply, other fields await a settled checkpoint/restart | Keep existing revision; immediate revocations still fence |
| Queued with SessionId already created | Recompile before dispatch; stale compilation cannot run | Keep existing revision and revalidate current authority |
| Idle | Apply live/checkpoint-safe fields; restart-required fields remain pending | Keep revision until a separate requested update |
| Suspended | Recompile and validate on resume; missing capability blocks | Restore old revision only if still authorized and compatible |
| New root or child session | Compile activated revision intersected with parent scope/budget | Compile activated revision intersected with parent scope/budget |
| Existing descendant of older session | Its own application boundary/receipt is required | Retain its revision; no retroactive grant widening |

For this proposal, creation means the canonical SessionId creation sequence, including a newly
spawned child. A queued task without a session is new when assigned one; a new turn or resume on
an existing session is not a new session. Concurrent edit/spawn order follows canonical sequence.
An old parent's context may be retained, but cannot authorize stale credentials or larger grants.
No permanent live linkage or universal inheritance operator is selected.

The UI previews affected sessions and fields before an admitted edit and makes delayed/blocked
application visible afterward. Cursor SDK resume options and ACP additional roots illustrate why
settings cannot be assumed to survive reattachment (SRC-V3-152/SRC-V3-176). The configuration
trial exercises all states, both modes, revocation and concurrent spawn with exact receipts.

## Mark 2 Owner direction — 2026-09-06

OR-028 requires separately configurable Steer/Queue, Aggressive/Full/Lazy Stop and source-aware forks preserving two versions with before-work notice or after-work disclosure/review. OR-029 requires outstanding-session or new-session-only configuration propagation. OR-033 preserves six distinct update modes and requested repair. Exact bindings, operation boundaries, inheritance and Stop/PAUSE relationships remain research outputs in the [Mark 2 handoff](foundation-research/mark2/handoff.md); neither dirty-source refusal nor automatic hot reload is a universal default.

## First-release capability coverage — accepted 2026-09-06

OR-023 requires streaming output, questions, approvals, queued messages, active steering,
interruption, resume, forks, compaction controls and custom commands/plugins. See their
[observable behavior and ownership](foundation-research/platform-round/interaction-contract.md).
Probe the shared runtime, adapter, supported upstream extension, native loop and combinations
before treating a simple wrapper's missing feature as a platform limit. Preserve engine-specific
features through explicit versioned extensions; unsupported route combinations remain visible.

Configuration must cover engines, tools, context, memory, routing, delegation, verification,
scheduling, placement and resources. Account, project, profile, workflow and task composition
needs explicit precedence/merge rules and ceilings, effective-value provenance and lifecycle.
The hierarchy below is the existing starting contract; profile/workflow/account relationships
are proposed additions to validate before schema freeze. Setup/import/preview/guided repair
share one change transaction; first-auth and offline recovery stay model-free.

## One CLI over native and upstream loops — proposed accommodation

Recursion commands address canonical goals, plans, permissions, evidence and status in both modes.
A direct-API native loop would give Recursion control of sampling and tool orchestration.
A delegated route retains the upstream inner loop and uses only its supported interface.
Neither mode is assumed faster. See [compatibility.json](foundation-research/compatibility.json)
for layer ownership and explicit capability gaps. FND-004/FND-005.

Proposed command bridge:
- Reserve canonical commands. Import native commands under an engine namespace; aliases bind
  exact versions and argument schemas. Reject collisions and expose translation losses.
- Register Recursion custom tools with a supported MCP/dynamic-tool surface only after grant
  mapping. An upstream plugin or command origin confers no authority.
- Distinguish “queue next input,” “steer active turn,” and “interrupt.” Bind target turn and route
  epoch. If the engine lacks a control, show unavailable or offer an explicitly scoped native
  session; do not emulate successful control through blind keystrokes or model-generated prose.
- Render correlated question/approval events without polling a model. Local status/help and
  deterministic commands need no inference request. Use bounded incremental event rendering;
  measure bridge overhead separately from engine/provider time.
- Report native session outcomes separately from canonical acceptance. Native compact/resume/fork
  cannot silently overwrite the Recursion capsule, permissions or filesystem recovery state.

For Claude, keep SDK/API integration distinct from an unmodified Claude Code binary conducting
its user's own native sign-in. Current terms and exact interface coverage control eligibility;
Recursion must not collect upstream session tokens. Codex technical ChatGPT login support is not
a universal OAuth entitlement. `decision.foundation.authentication` remains pending. FND-006.

The binary route now has direct CLI sources, separate from SDK evidence: SRC-V3-139 through
SRC-V3-143. Certify binary, mode and configuration together. In particular, `--bare` excludes
subscription sign-in; `--safe-mode` retains normal authentication but suppresses custom MCP/hooks.
Neither is a ready-made governed profile. Native command availability varies with mode/version;
profile optimization must preserve the required interaction controls. See
`analysis.foundation.claude-bridge` and `experiment.foundation.adapter`.

Deterministic zero-model setup remains the bootstrap. Optional agent-assisted `/setup` may explain
presets and propose a scoped diff after first-auth; credentials stay in protected provider/OS flows.
Validation, explicit apply, rollback and offline recovery must still work without the assistant.
This is a product proposal, never authorization for an agent to modify its own configuration.
`decision.foundation.setup` and `experiment.foundation.setup` remain pending. FND-012.

## Product requirement

The first CLI is the canonical local operator surface for the headless v3 harness. It must let
an owner configure and inspect Recursion without asking an agent to edit files or call commands.
Telegram is an optional interactive transport, not a replacement configuration authority. No
graphical frontend is required for the v3 release. The same design should remain teachable
enough for later public users.

Amendment proposed 2026-10-24 (acceptance is a human decision by Lucas): D-038 already
superseded "No graphical frontend is required" as a release-exclusion (built but opt-in;
no capability deferred). This amendment does not reopen D-038. The CLI remains the
schema-driven editor and a complete operator surface. **Beyond D-038** a daemon-served GUI
in a thin Tauri shell is a client of the same command schema. Agent-installable setup
(`doctor --json`, Control MCP) is added; an agent is still never the only way to configure.
See [AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md).

The CLI is a schema-driven editor, validator, explainer, and control surface over human-readable configuration. It is not the only copy of settings and does not hide policy in an opaque database.

## Interactive and detachable operation

Running `recursion` without a one-shot subcommand opens the interactive general view. Additional
terminals may attach to Head, a selected ARM, worker/session output, the communication graph,
events or system state. Terminals are replaceable clients of the daemon: closing one detaches
its stream and leaves the actor/process running. Explicit pause, stop, kill or daemon commands
control lifetime.

The interactive UI and one-shot commands compile to the same versioned command schema. A
terminal widget, prompt buffer or operating-system window never becomes canonical state.
Attaching a view must not launch an additional model process, plugin or repository indexer.
See [TERMINAL-RUNTIME.md](TERMINAL-RUNTIME.md).

## Configuration hierarchy

```text
built-in schema defaults
  < edition preset
  < installation/authority-domain config
  < project config
  < parent-goal config
  < task override
  < explicit ephemeral operator override
```

The most specific valid value wins. Child scopes may narrow hard bounds. Widening authority, privacy exposure, credentials, network, spending, or publication rights requires an explicit acknowledged operation at that scope.

Configuration contains credential references only. Machine-private configuration remains outside the public source checkout; projects may carry safe `.recursion.yaml` files whose requested capabilities are admitted by the owner/controller.

### Resolution and mutation contract

One versioned schema drives file, CLI and transport editors, but schema validation is not the
authority decision. A deterministic semantic validator resolves scope in the order above,
proves every lower-scope authority/limit is a subset unless an authenticated widening command is
present, validates cross-record route/plugin/GoalSpec dependencies, and emits the effective
value plus provenance for every field. Unknown fields and invalid combinations fail before a
side effect.

An edit carries schema version, configuration scope/id, expected current version/digest,
canonical before/after representations, principal, authority receipt, application lifecycle and
idempotent command id. Stale-version edits fail rather than using last-write-wins. Canonical
serialization is required before digest/signature comparison; human-readable TOML/YAML spelling
is never itself an authority token.

Configuration complexity is reported as a vector: field count, scope levels, cross-field
constraints, lifecycle transitions, authority/billing/privacy edges, plugin/provider/model/host
conflicts, invalid-combination classes, explainable-rejection coverage, valid round-trip coverage
and migration/rollback coverage. Pairwise and three-way covering fixtures test representative
core, standard, remote, full and custom manifests. A weighted aggregate may summarize only after
every security, privacy, billing and correctness floor passes.

The schema may use [JSON Schema 2020-12](https://json-schema.org/specification) for structural
validation and [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785.html) for a canonical JSON
digest/signature representation. Neither replaces the semantic authority/subset validator.
TOML 1.0 or YAML may remain the human file syntax only when round-trip, duplicate-key, type and
normalization fixtures prove that the compiled canonical representation is deterministic.

## Setting lifecycle

Schema metadata classifies every field as:

- `goal-frozen`: snapshotted into GoalSpec and changed only by amendment;
- `checkpoint-applied`: takes effect at the next machine-valid checkpoint;
- `live`: safe operational control such as lower concurrency or notification preference;
- `restart-required`: daemon/plugin lifecycle change.

Restrictions may tighten immediately. A command explains effective value, provenance, validation, and application time before making a change.

## Candidate command surface

```text
recursion
recursion attach head|arm|task <id>
recursion workspace open [layout]
recursion view graph|events|system
recursion detach
recursion daemon status|start|stop|restart
recursion init
recursion configure [--profile ...]
recursion config get|set|edit|validate|diff|apply|explain|migrate
recursion provider add|auth|probe|disable|list
recursion model list|show|suite|pin
recursion pool list|status|reserve|disable
recursion route explain|simulate|probe
recursion node enroll|list|show|revoke|doctor
recursion project add|list|show|classify|doctor
recursion goal grill|draft|accept|amend|status|stop
recursion arm list|show|checkpoint|replace|pause|resume
recursion arm stop|kill <id>
recursion queue list|explain|prioritize
recursion context inspect|compact|rehydrate|doctor
recursion evidence show|verify|export
recursion research start|status|claims|sources|gaps|verify|export
recursion branch inventory|explain|prune-safe
recursion doctor [--quick|--full|--component ...]
recursion pause|resume
recursion observe|status|events|budget
```

Names are hypotheses. Research should compare established CLI conventions in the reference repositories and test discoverability.

## Zero-model configuration wizard

`recursion init` and `recursion configure` render from the versioned configuration schema. They:

- discover installed CLIs and supported local capabilities without reading secret values;
- offer minimal, standard, remote, full, and custom manifests;
- explain subscription versus metered billing boundaries;
- show privacy/egress implications;
- validate provider-native authentication in place;
- write only the requested config scope;
- produce a before/after diff and rollback file;
- require no model call.

Advanced users retain manual file editing. CLI and file validation are generated from the same schema so they cannot drift.

## Doctor tiers

- `startup`: cached, zero-token checks necessary to use enabled features; target seconds.
- `quick`: configuration, writable paths, SQLite health, Git, enabled plugin manifests, provider executable presence, and current PAUSE.
- `component`: focused provider, node, route, context, gate, or plugin diagnostics.
- `full`: release/conformance suite; never an ordinary startup dependency.

Successful checks are cached against all relevant hashes and invalidated precisely.

## Explainability

`route explain`, `route simulate`, `queue explain`, `capacity plan`, and `config explain` do not invoke a model. They show:

- candidate routes and rejection reasons;
- inherited and overridden settings;
- quality/cost/quota/privacy/resource inputs;
- current reservations and waits;
- checkpoint at which a change applies;
- whether an action crosses a billing or authority boundary.

## Full-access UX

The CLI must expose owner-authorized full-access and provider-native dangerous modes without
pretending that a restricted preset is absolute. It should display and configure a capability
vector including filesystem/process/network, control-plane authoring, credential administration,
provider enrollment, spending limits, publication, merge, deployment, service control and any
permission to alter ordinary PAUSE defaults.

A maximum `sovereign` preset may select every supported capability, including `grant-admin`.
When that capability is explicitly enabled, the actor may mutate its own grants through a
schema-checked CLI/kernel action. Each mutation shows the exact before/after blast radius,
requires the configured authentication and acknowledgement, emits a durable receipt, and offers
a reversible downgrade where the external effect permits one. Audit retention/deletion remains
a separate displayed capability. This product behavior requires constitutional reconciliation
before use in the current private deployment.

## PAUSE and capacity UX

Under the separately proposed future post-amendment profile, `recursion pause` defaults to the
Owner-selected `checkpoint` policy and accepts explicit `interrupt` or `drain` alternatives,
explains which current operations may finish, and separately exposes emergency stop.
`recursion status` must show the applied policy, PAUSE epoch, in-flight operations and any
quarantined ambiguous effects.

That is a future v3 surface, not current private behavior. Until constitutional amendment,
compatibility mode exposes the current immediate halt and must not relabel it as checkpoint.
Every displayed command binds the operative constitution-policy reference and shows whether the
requested behavior is unavailable under it.

Branch and worktree admission is driven by measured machine and review-backlog capacity. The
CLI shows the inferred limit, contributing signals, hysteresis, owner hard ceiling and why a new
branch was admitted or refused. Seventy-five is historical policy input, not a universal v3
constant.

## Stable observer interface

Scheduled tasks and external assistants receive a versioned read-only interface for status, events, budgets, blockers, health, and evidence references. Scoped control is a separate authenticated capability. This lets monitoring tools check Recursion without parsing terminal prose or receiving implementation authority.
