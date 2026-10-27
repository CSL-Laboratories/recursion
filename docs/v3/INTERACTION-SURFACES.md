---
title: Recursion v3 interaction surfaces and collaborative ARM topology
status: amendment proposed (2026-10-24); acceptance is a human decision by Lucas
last-updated: 2026-10-25
prior-status: accepted-direction-with-open-research
---

# Recursion v3 interaction surfaces and collaborative ARM topology

## Installed interaction fixture — 2026-09-07

IPR-002: Codex 0.149.0 App Server, driven by a scripted loopback provider, rejected a stale
turn steer and an in-flight fork; delivered an accepted steer into the same turn; interrupted
a blocking question; kept a late old answer from releasing the current waiter during the
observed 300 ms window; and completed the current question with its own answer. A completed-turn
fork retained exactly its selected boundary. An idle-server crash/restart preserved the original
three turn identities and their completed/interrupted/completed statuses.

This is exact-build evidence for the adapter mechanisms, using experimental environment and
question fields. It does not establish cross-connection correlation, native approval handling,
post-resume execution authority, in-flight crash recovery or the complete questionnaire-to-Head
product workflow. Keep canonical consultation/goal/lease/operation identities outside provider
session IDs, and reapply current grants before resumed execution. Preserve all ten controls,
three Stop modes, persistent assistants and Owner-visible attribution requirements.

The [finite consultation vectors](construction/interface-probes-20260907/contract-vectors.json)
separately require local Stop to expire the old reply alias, preserve evidence received earlier,
prevent stale adoption/relaunch and allow a freshly authorized consultation on another operation.
Those modeled results do not certify remote cancellation or an implemented communication service.

## Mark 3 comparison: consultation and protocol placement - 2026-09-07

Compare Head relay, direct addressed consultation and a shared room with one bounded step lease
using the same questionnaire-assistant-to-Head fixture. Each preserves attribution, permitted
context/evidence exchange and Owner observation, participation, steering and Stop. Direct
addressing is the first probe target, not a mandated default. Relay can duplicate reasoning;
shared discussion adds conflict/cycle handling. Human coordination burden and final answer
quality belong in the comparison. CMP-012.

Use an application-owned consultation envelope and durable event stream across all topologies.
ACP is a client-agent session adapter; A2A is an optional opaque remote-task bridge. Neither
protocol's session/context identifier replaces GoalSpec, current role lease or parent accounting.
Protocol acceptance, delivery, answer and authoritative adoption are separate receipts. A2A
send idempotency is optional, so the outer command identity must handle duplicates. ACP
load/resume and additional-root behavior must be bound to negotiated capabilities.

Owner views render the same durable events with speaker, role, route, goal/context revision,
evidence and adoption state. A host-generated permission reply records its actual principal;
it must not imply human observation. Stale turn/lease answers and inputs receive typed conflicts.
A slow or absent view cannot block Stop, canonical append or recovery. Control ingress has
reserved bounded capacity; presentation may coalesce only after durable append. Replay gaps
require RESET before new EVENT delivery. CMP-013, CMP-014.

The detailed [bounded probe plans](foundation-research/mark2-research/mark3-comparison/probes.json)
retain all ten terminal controls, preserved forks, three Stops and remote uncertainty. No
synthetic document check or protocol method name establishes observed interaction support.

Keep logical consultation, reply-delivery generation, operation ownership and authoritative
adoption lifetimes distinct. A logical wait must not occupy the only executor needed by its
consultant or block a read pump that must receive the answer. Reply aliases are useful prior art
for invalidating future delivery, but do not cancel remote computation or erase already received
messages. Full Stop of a borrowed consultant operation preserves unrelated persistent-assistant
work. Owner views expose expiry, stale advice, missing evidence and the next permitted action.
CMP-021.

Reserve end-to-end control progress before SDK ingestion, through framing/parser, transport
window, dispatcher, canonical writer and rendering. A bounded SDK notification queue can drop
a fact before Recursion sees it; require a sequence/query recovery path or keep it unknown.
Cancellation signals cannot serve as evidence of termination. Record offered arrival, submission,
canonical acceptance, recipient fence installation, cancellation delivery and settlement
separately so stalled generators and sinks cannot hide Owner-visible delay. CMP-020.


## Required visible assistant-to-Head workflow — 2026-09-07

OR-038 requires an Owner to work with a questionnaire assistant that can consult Head directly,
exchange relevant questions/answers/evidence, and return clarification to the ongoing
conversation. The Owner can observe who requested, replied and adopted a decision; participate,
redirect and stop the exchange. Head integrates exact approved answers and continues research
toward concrete architecture and bounded delegation.

This capability can cross compatible model/harness sessions, terminal views and configured
execution nodes. Preserve goal/version, current grants and leases, decision provenance,
causation and shared accounting. A view is an authorized projection, not process ownership.
Show accepted, delivered, adopted/declined/expired and settled/unknown states truthfully.
Consultation is not permission to amend GoalSpec or direct a peer outside existing grants.

Compare Head-routed, direct addressed and shared-discussion implementations under the same
required behavior. No topology, channel or transport is universally selected by OR-038.
Research chooses mechanisms, activation and defaults while retaining the accepted communication
capability and meaningful Owner control. Cognitive/coordination burden and clarity are outcomes,
not costs to leave outside a narrow quota benchmark.

## Visible work without fabricated activity — research proposal

Prioritize event timelines, diffs, task dependencies, accepted state transitions, truthful
uncertainty and actionable blockers in CLI-first v3. Distinguish engine activity from canonical
acceptance; a busy spinner is not progress. Expose provider reasoning text/summaries only where
returned, label opaque state and counters honestly, and never reconstruct hidden reasoning.

Comprehension, accessibility, input stability, privacy and rendering/resource cost need separate
tests. No automatic private export, graphical/media extension or viral-adoption claim is accepted
by this research. See `experiment.foundation.resources` and `experiment.foundation.setup` in
[experiments](foundation-research/experiments.json). FND-017.

## Release surface ruling

Recursion v3 is its own headless development harness with optimization as an acceptance target,
not a measured property. It owns the durable actor
runtime, goal/delegation protocol, state, routing, context, tools, evidence, policy and
observer contracts even when an individual model turn runs inside an external CLI or agent
loop. Reusing an optimized provider harness is an engine choice, not surrender of Recursion's
product boundary. Under `OR-019`, no such engine is a canonical boot or recovery dependency.

The v3 release requires no web, desktop or mobile graphical client. Its owner-facing surfaces
are:

- an interactive Recursion CLI as the canonical local configuration, inspection and control
  surface, including detachable multi-window views;
- Telegram as an optional interactive transport for conversation, commands and monitoring;
- the versioned Observer Interface for read-only automation and future UIs.

Amendment proposed 2026-10-24 (acceptance is a human decision by Lucas; F-2 answered by
D-038): the sentence "requires no web, desktop or mobile graphical client" is already
superseded by D-038 (built but opt-in; no capability deferred). This amendment does not
reopen D-038. **Beyond D-038** owner-facing surfaces add a daemon-served web GUI in a thin
Tauri shell (Linux, Windows, macOS), a standalone app, and phone/web remote clients on the
same authenticated Control API as the CLI/TUI and Observer Interface. CLI-only use remains
valid. Default remote relay is research question F-9. See
[AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md).

Telegram and CLI state are projections over the same actor/event model. Chat history, topic
identity and provider sessions are never canonical task state.

Multiple terminals can attach to Head, an ARM, a worker, a verifier, a communication graph or
system state. Closing a terminal detaches the view without ending work. The persistent daemon
and managed process broker own actors and process trees; deliberate pause/stop/kill commands own
termination. The detailed proposal and research gate are in
[TERMINAL-RUNTIME.md](TERMINAL-RUNTIME.md).

## Accepted conversation topology

### General surface

The conceptual `#general` channel is the direct conversation with the Recursion Head. It supports
macro planning, Grill Me, goal scoping, ARM creation, portfolio monitoring and escalation.
The name is a presentation default, not a hard-coded protocol identifier.

Head is the last automated semantic escalation level. A question that remains unresolved
after the final approved Head route is recorded for the owner and included in the configured
digest. Route exhaustion does not fabricate an answer.

### ARM surfaces

An ARM is a goal-scoped delegated executive that achieves its GoalSpec by planning and
delegating a bounded fleet of workers, reviewers and harness sessions. One ARM per repository
is the expected common configuration, not an identity invariant: a goal may span repositories,
and a repository may host serial or explicitly independent ARM runs.

Each active ARM may expose a dedicated Telegram topic or equivalent addressable conversation.
The owner may talk to that ARM without relaying every message through `#general`. An accepted
direct instruction becomes an attributable event under the ARM's existing GoalSpec; it cannot
silently create an unrelated goal or widen privacy, spending, credentials, publication or
deployment authority. A widening instruction is routed through amendment or escalation.

Head remains aware through canonical events and monitoring, not by copying every chat message
into Head's model context. Direct ARM access therefore reduces conversational bottlenecks
without making the ARM independent of the constitutional hierarchy.

## Required multi-ARM collaboration; topology comparisons

The referenced product is the official [SpaceXAI Grok Bot](https://docs.x.ai/grok-bot/overview),
opened 2026-09-03. Its documentation defines a Bot as one persistent named agent, says multiple
Bots can run in parallel on one user-scoped computer, message each other, share threads or group
chats and pass ownership. That is evidence for the interaction pattern Lucas referenced, not
evidence that shared-computer credentials, transcript-order authority or an unbounded handoff is
safe for Recursion. The public [installer repository](https://github.com/grok-bot-app/grok-bot)
describes a separate “Chief of Staff” role;
Recursion does not adopt that role because Lucas rejected a distinct AI ARM Manager.

Compare these topologies while preserving the required communication capability:

1. **Head-routed room:** Head owns the shared conversation lease, solicits named ARM input and
   commits one reconciled transition. This remains a candidate default to compare, not the sole required path.
2. **Event-mediated peers:** ARMs exchange typed requests and evidence through the kernel while
   one named ARM may hold a bounded, expiring coordination lease for each cross-ARM workflow
   step. The owner has accepted this topology when the lease cannot change room membership,
   overall goal, authority envelope, conflict policy or ARM lifecycle.
3. **Direct peer delegation:** an ARM delegates to or directs another ARM. Any such delegation requires existing grants and evidence for authority, budget
   accounting, deadlock handling and one-writer ownership without a hidden executive tier.
   Direct consultation does not imply peer command authority; OR-037 adds no amendment rights.

A workflow `supervisor` may be a temporary coordinator role held by Head or a named ARM inside
one GoalSpec. It is not a new constitutional AI ARM Manager and may not acquire portfolio
authority merely because several actors share a room.

Head retains ownership of the overall goal, room membership, authority envelope, conflict
resolution, ARM lifecycle and a substantial part of strategic model escalation even while a
named ARM holds the step lease. Lease expiry, revocation, PAUSE or conflict returns coordination
to Head without changing actor identity or treating transcript order as a handoff.

## Required interaction semantics

- Every visible message identifies the speaking logical actor, selected route and GoalSpec.
- Addressing syntax and Telegram topics route messages; they do not grant authority.
- Owner messages are preserved byte-exact or by immutable reference before summarization.
- Multi-actor rooms separate proposals, accepted commands, evidence, status and narration.
- Only the current executive lease holder may author an authoritative plan transition.
- Cross-ARM requests carry causation, correlation, source, budget and return-channel identity.
- Mute, archive, topic deletion, transport outage and bot replacement do not delete the ARM.
- Head monitoring and owner digests are derived from canonical state, not transcript scraping.
- A compromised or removed Telegram bot token affects one transport capability, not actor
  identity, credential inventory or canonical state.

## Evidence-grounded transport contract

Telegram Bot API 10.3, opened 2026-09-03, exposes forum-topic `message_thread_id` values and
separate new/edited-message updates. Its `update_id` is useful for duplicate and ordering
recovery, but changes after a week without updates; it is therefore a transport cursor, not a
permanent Recursion event sequence. Incoming updates are retained by Telegram for no longer than
24 hours, and long polling and webhooks are mutually exclusive. A webhook can carry a configured
secret-token header, but transport authentication does not authorize an actor transition.

Each accepted inbound message is normalized before an executive sees it:

```text
transport + bot identity + chat/topic + update/message/edit identity
  -> authenticated owner/actor endpoint mapping
  -> immutable raw-message reference and receive time
  -> GoalSpec + actor + authority + PAUSE + lease validation
  -> typed proposal, command, evidence, status or narration event
  -> one idempotent acceptance/rejection receipt
```

Edits and deletes are new transport observations; they never rewrite an accepted command or
historical owner-intent event. A later correction uses an attributable amendment. Unknown,
duplicate and out-of-order delivery is harmless until this normalization succeeds. Topic
creation/deletion and bot replacement change routing metadata only.

The default free Bot API guidance is roughly one message per second in one chat, 20 per minute
in a group and about 30 per second for bulk broadcasts. Paid broadcasts are a separate Telegram
Stars spending capability. Recursion therefore needs per-chat and global queues with backoff,
coalesced status output and an explicit denial of paid-broadcast fallback unless the exact
GoalSpec and grant authorize it. Presentation throttling must not delay the canonical PAUSE or
authority event.

These transport facts are pinned to [Telegram Bot API
10.3](https://core.telegram.org/bots/api) and the official [Bot
FAQ](https://core.telegram.org/bots/faq), opened 2026-09-03. They expire with an API or policy
change and do not prove Telegram availability, entitlement or Recursion message correctness.

### Shared-room step lease

A `CoordinationLease` binds `room_id`, `workflow_step_id`, named holder, participating actor ids,
allowed message/event verbs, budget slice, issue/expiry/revocation epochs and a fencing token.
Only the current token may propose the step transition. It cannot add members, alter GoalSpecs,
transfer itself, settle cross-goal disputes or change the conflict policy. Simultaneous valid
proposals remain evidence until Head accepts one transition; transcript arrival order is never
the tie-breaker. Expiry, PAUSE, holder replacement or conflicting owner instruction returns the
step to Head and quarantines any ambiguous outward effect.

## Research and acceptance questions

Before this becomes a construction contract, research must establish:

1. Which official Grok Bot interaction patterns remain useful after rejecting shared ambient
   credentials, transcript-order authority and a distinct Chief-of-Staff executive tier.
2. Whether one bot identity with actor routing or multiple bot identities gives better
   isolation, usability, credential scope and operational resilience.
3. How shared rooms resolve simultaneous commands, disagreement, cycles and cross-goal budget
   ownership without transcript order becoming authority.
4. Which events Head must observe immediately versus summarize for a digest.
5. Whether direct ARM conversation measurably reduces owner effort without increasing lost
   context, contradictory instructions or unreviewed work.

Minimum acceptance requires synthetic multi-ARM conversation fixtures, duplicate/out-of-order
delivery, edited/deleted messages, transport restart, token revocation, PAUSE, GoalSpec
amendment, actor replacement and a proof that no chat topology creates a second portfolio
executive.
