---
title: Recursion v3 interactive terminal and persistent session runtime
status: amendment proposed (2026-10-24); acceptance is a human decision by Lucas
last-updated: 2026-10-25
prior-status: accepted-direction-with-open-research
---

# Recursion v3 interactive terminal and persistent session runtime

## Conditional replay-store mapping â€” 2026-09-07

RSM-002: a genuine gap can use HELLO(requested cursor), WELCOME(retention floor, snapshot cursor),
RESET(snapshot cursor), fresh CREDIT and the next EVENT. The narrow controls require requested
cursor < retention floor <= snapshot cursor, recorded modeled retention loss and an available
snapshot. After WELCOME detects the gap, any non-RESET continuation remains refused.

M1B validates the wire relationships; it cannot authenticate a claimed replay floor or prove that
a snapshot exists. The mapping controls retain
a wire-only forged-fact control that passes the oracle while separate fact guards reject missing
loss/snapshot inputs. Those guards operate on modeled inputs, not authenticated store evidence.
No gap may be invented to recover an event that was merely deferred locally. Physical retention,
snapshot integrity, SDK loss detection and reconnect provenance remain unimplemented.

## Replay and flow recovery distinction â€” 2026-09-07

FMC-001: the immutable [M1B wire profile](M1B-WIRE-PROFILE.md) requires RESET before any further
same-stream message after detecting a replay gap. ACK, CREDIT, INPUT, RESIZE, HELLO, WELCOME and
SNAPSHOT cannot bypass that rule; an intervening message permanently refuses the trace even when
RESET follows. Ordinary credit exhaustion is distinct: it remains unrecovered until RESET but
does not itself impose the replay-gap-continuation refusal. Missing CREDIT also preserves its
own permanent refusal. Event cursors advance by exactly one; ACK cursors cannot move backward.

Owner Stop is outside the closed M1B broker-message union. The formal slice assumes a separately
typed direct control path and checks its progress while global admission, a downstream stage or
a partial-frame buffer is full. This does not demonstrate a physical reserve through ingress,
framing, scheduling, canonical writing and view delivery. Signaled loss handling also does not
establish real SDK loss detection. These remain explicit implementation and mapping assumptions.

## Observed Windows launch gap â€” 2026-09-07

IPR-001: the bounded Windows probe reproduced the create-suspended-before-association gap.
The observed live child was outside the operation Job and survived broker death. The
`PROC_THREAD_ATTRIBUTE_JOB_LIST` treatment placed the child in that Job at creation; observed
children terminated when broker death closed its last handle. Later association and ordinary
descendant checkpoints also terminated their observed children. An independent outer safety
Job contained every experiment and was empty after cleanup.

Disposition: use supported creation-time containment as the Windows launch candidate, with
membership verification before resume and explicit refusal on association failure. This result
does not cover a crash inside the OS call, alternate launchers, hostile escape, Job breakaway,
registration/reconciliation, remote effects or Full Stop. The at-birth created/associated
checkpoints repeat the same membership boundary; they are not two independent mechanisms.
Linux cgroup launch and remote recovery remain separate admissions. The test's Windows Job
around a WSL client alone does not prove Linux cleanup; successful Hermes runs additionally
observed the owned Linux namespace's exit.

## Mark 3 comparison: birth containment and control progress - 2026-09-07

PTYs carry terminal data; they do not define canonical ownership or Full Stop. Compare Windows
Job Objects and Linux delegated cgroup scopes with independently admitted restart suppression.
The inspected node-pty and portable-pty kill/session paths do not prove complete owned-subtree
settlement. Job breakaway, escaped processes, alternative creation and missing delegation remain
explicit failures or admission blockers. CMP-002, CMP-004.

Compare post-creation association with supported containment-at-birth APIs: Windows
PROC_THREAD_ATTRIBUTE_JOB_LIST and Linux clone3(CLONE_INTO_CGROUP). Crash the broker at creation,
association, registration and immediate-child boundaries; require no child or an identifiable
contained child. Closing the last kill-on-close Job handle has an execution policy consequence
separate from detaching a view. No host/build or process behavior was tested here. CMP-025.

Control reserve must survive every stage: external ingress, framing/byte cap, transport window,
read pump, callback scheduler, canonical writer and view projection. The pinned MCP Python SDK
can evict vendor notifications before application ingestion, and its partial stdio-line buffer
has no observed local size cap. Inline handlers can deadlock the pump. Require queryable event
sequence/gap repair or explicit unknown for missing authoritative facts; never invent lost
settlement. A slow log consumer can block a Windows pipe writer, so freeze sink/drain behavior
in headless/TUI comparisons. CMP-020.

Expose offered input, accepted command, installed recipient fence, delivered cancellation,
owned termination and external settlement separately. Reconnect repairs projections and
subscriptions; it does not relaunch effects or certify offline termination. Required ten
controls, Aggressive/Full/Lazy Stop, preserved forks and input/resize leases are retained by
the [comparison probes](foundation-research/mark2-research/mark3-comparison/probes.json).

## Mark 3 multiplexed interaction and control â€” 2026-09-07

OR-038 requires persistent attachable views over durable work, including the questionnaire
assistant/Head exchange. Multiple windows or clients must not duplicate execution or inference.
Compare per-view cursors, bounded projections and replay/snapshot recovery; protect control
delivery from slow rendering, disconnected views and bulk history replay. Input ownership,
operation identity, causation and receipt state must be visible.

Owner participation, steering and Aggressive/Full/Lazy Stop remain distinct across sessions and
configured nodes. An acknowledgment is not remote settlement; retain partition and external
effect ambiguity. Measure Unicode/render correctness, accessibility, input tail latency,
attach/detach/reconnect and time to understand state and issue the intended control.

Source-audit counterexamples reinforce the need for composed control mappings: Pi queues may
survive RPC abort; Hermes busy-input redirection may preserve foreground work in background;
a host can answer an ACP approval without a human prompt. See SRC-V3-226, SRC-V3-227 and SRC-V3-228 and
[the bounded source audit](foundation-research/mark2-research/mark3/companion-review.json). These are unrun
adapter tests, not a new Stop definition or installed-build certification.

## Mark 2 control contract â€” proposed 2026-09-07

The ten controls remain required across the platform. The canonical command includes command ID,
target task/session/attempt/turn, expected epoch/config revision, authority and delivery policy.
Persist acceptance before presentation; report received, delivered, settled, rejected or uncertain
as distinct dispositions. A transport acknowledgment never proves semantic compliance.
MKR-011/MKR-012; [feature-level source bindings](foundation-research/mark2-research/compatibility.json).

| Control | Observable contract and decisive negative trace |
|---|---|
| streaming | Ordered task/turn events with bounded client credit; slow client receives replay or RESET + GAP without blocking canonical writes |
| questions | Durable question ID, target waiter, expiry and answer/cancel; reconnect or delayed answer cannot target a replacement question |
| approvals | Exact action/arguments/scope and current grant; reuse valid prior authorization, reject stale or changed-action replies |
| queued_messages | Durable enqueue/edit/remove before delivery; crash after dispatch before acknowledgment cannot duplicate input |
| active_steering | Target current attempt/turn and report actual delivery; queued follow-up remains explicitly different |
| interruption | Distinct Stop mode/scope plus admission fence; report surviving or uncertain work |
| resume | Validate checkpoint, effective config, route, grants and unsettled effects before continuation |
| forks | Preserve original unfinished work and separate derived version; conserve parent allowance and disclose chosen timing |
| compaction | Inspect/request/tune only supported transforms; retain directives, waiters, evidence and fallback rehydration |
| commands_plugins | Namespace, supplying component, schema/config revision and authority visible; reject collisions and lossy behavior claims |

| Stop mode | Boundary and scope | Required evidence |
|---|---|---|
| Aggressive | Forcefully interrupt selected agent without waiting for its current operation; fence further selected-agent dispatch | Local termination/control observation and uncertain in-flight effects; descendants are listed separately, not silently included |
| Full | Stop selected agent and its owned subagents, shells, monitors and relaunchers; scope is a rooted ownership set | Every local owned process settled or reported unresolved; remote hosts need separate receipts |
| Lazy | Close admission and stop at the next observable current operation boundary (tool completion, protocol turn boundary or validated checkpoint) | Boundary identity and deadline; waiting/timeout visible, configured escalation explicit |

Aggressive can sacrifice the current operation. Full also fences supervisors and returning workers
so killing a PID cannot cause an unnoticed restart. Lazy never measures hidden reasoning or
claims an invisible train of thought completed. These future controls do not change current
immediate operational PAUSE. A shared server may own unrelated sessions: revoke the selected
session's capability and use a documented session primitive; killing unrelated work is not an
automatic consequence of Full. If containment cannot isolate the requested scope, report it.

Windows Job Objects and Linux cgroup v2 are candidate process/accounting boundaries, with
breakaway, alternate process creation, cgroup migration and privilege escape as negative traces.
systemd restart policy must be fenced through its owner, not only its main PID. MCP/ACP cancel,
Codex interrupt and socket closure are protocol observations; none proves external rollback.
Remote acceptance without receipt remains ambiguous under M1C, forbidding duplicate retry.
SRC-V3-172 through SRC-V3-179; MKR-011.

A preserved fork records source HEAD, index, tracked dirty bytes, scoped untracked/ignored
artifacts, context/evidence/config revision, source cursor, lineage and budget split. Acquire a
cooperative snapshot boundary or validate a repeated manifest around capture; a changed source
forces retry/refusal of that snapshot, not a universal dirty-tree ban. Treat symlinks, binaries,
submodules, large/ignored files and excluded secrets explicitly. No cleanup, stash push,
truncation or merge is implied. Preserve an anchored immutable snapshot before derived writes.
Conversation fork, worktree creation and filesystem snapshot are three distinct operations.
SRC-V3-144/SRC-V3-195/SRC-V3-206/SRC-V3-207.

The configured notice-before or disclosure-and-review-after policy records source state,
capture boundary, preserved original and derived paths, exclusions and subsequent divergence.
A sibling receives a separately reserved share, not a copy of the parent's allowance.
Probes must demonstrate original bytes unchanged, coherent context/artifact binding, duplicate
command idempotency and rejection of stale leases under concurrent edits.

## Engine process and remote-operation distinctions

Native Windows/Linux remain acceptance targets. Broker-owned processes, daemon-owned canonical
state and detachable views are already specified; attaching a view must not require another
model request. Terminal bytes do not establish structured turn identity, billing completeness,
effect mediation or rollback. A restricted passthrough route cannot silently replace a governed
adapter. Preserve separate process/daemon, stream cursor, input lease and canonical fence domains.
FND-003/FND-004.

Current upstream documentation includes Codex remote operation on connected Windows/Mac hosts.
Cursor Remote Control, Cloud Agents and My Machines have different start/host requirements.
Observe, attach, resume, steer, start, approve and unattended boot need separate capability records.
Sleeping hosts, stream loss and partial descendant termination remain explicit fault cases;
reconnection alone does not reauthorize work. See [compatibility](foundation-research/compatibility.json)
and `experiment.foundation.remote`. FND-013.

## Product ruling

The v3 CLI is interactive, not only a collection of one-shot commands. It must support several
simultaneous terminal windows or panes showing Head, ARM, worker, verifier and system activity.
Closing a terminal detaches that view; it does not terminate the actor, goal, external engine
or daemon. A deliberate control command ends, pauses or kills work according to policy.

This terminal experience is the v3 visual foundation. A later graphical frontend should be a
new client of the same versioned observer/control protocols, not a rewrite of runtime state or
actor semantics.

Amendment proposed 2026-10-24 (acceptance is a human decision by Lucas): D-038 (accepted
2026-10-02) already superseded the "later graphical frontend" exclusion — GUI and mobile
are built but opt-in in the first release; no capability is deferred. This amendment does
not reopen D-038. **Beyond D-038** the GUI client is a daemon-served web UI in a thin
Tauri shell on Linux, Windows and macOS, on those same observer/control protocols. The
protocol rule stands: the GUI must not rewrite runtime state or actor semantics;
window-close is detach, not Stop. See [AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md).

## Process and view topology

```text
Telegram topic     Terminal: #general     Terminal: ARM A     Terminal: worker/gate
      |                     |                    |                     |
      +---------------------+--------------------+---------------------+
                                    |
                        versioned local client protocol
                                    |
                              recursion daemon
             actor leases | events | policy | routing | terminal broker
                 |                 |                    |
           canonical state   bounded live streams   managed process groups
                 |                                      |
          Head / ARM runs              external CLI, worker, verifier, tool process
```

Terminal windows are views, not process owners. Actor identity, model sessions, work and
messages survive client attach/detach. One actor may have several attached views, and one
terminal may display several actors. The runtime must not launch another model or indexer merely
because a second view attaches.

## Architecture alternatives

1. **Daemon/broker plus thin terminal clients â€” ratified ownership boundary, implementation unselected.** Recursion owns actor/process lifetime,
   IPC, event cursors and recovery; any terminal can attach. This is portable and gives a future
   GUI the same protocol, but makes daemon/process-broker correctness critical.
2. **Terminal multiplexer as runtime.** Build directly around tmux, Zellij or an OS terminal
   application's panes. This is fast to prototype and inherits mature detach behavior, but makes
   one external UI/process model a product dependency and complicates Windows/Linux parity.
   Multiplexers remain useful optional view-launch adapters.
3. **One actor process per terminal.** Simple mental mapping, but terminal close kills or
   orphans work, duplicate views duplicate execution, and recovery becomes shell-specific.
   Reject as the canonical architecture.

Pinned implementation review supports the first option's ownership boundary without selecting
an implementation: Zellij supplies
the strongest inspected bounded multi-client/fair-writer pattern; tmux supplies live
consumer-offset backpressure; Ratatui plus crossterm is the leading native Rust renderer/input
candidate; and xterm.js is the leading browser renderer/accessibility candidate. Each remains a
replaceable presentation or transport dependency. Exact revisions and gaps are registered in
[SOURCE-REGISTER.md](SOURCE-REGISTER.md#frontier-harness-campaign-sources).

## Candidate interactive views

- **General:** Head conversation, active goals, ARM summaries, escalations and owner digest.
- **ARM focus:** direct conversation, GoalSpec, plan/checkpoints, child graph, blockers, budget
  and route.
- **Worker/session:** streamed model/tool output plus exact task, process and worktree identity.
- **Communication graph:** actors and current typed requests, with selection opening the
  corresponding event/message stream.
- **Event/log:** filtered canonical and operational events with correlation and causation.
- **System:** resource pressure, route/pool state, leases, PAUSE, quarantines and process tree.

An OS terminal window, tab or pane may host any view. `recursion workspace open` may propose or
launch a saved layout through an optional terminal-integration adapter, but the product must
also work through ordinary independent terminals and remote shells.

## Interaction model

The default full-screen experience should combine command entry with streaming status and
conversation. One-shot commands and a line-oriented fallback remain available for scripts,
screen readers, unsupported terminals and incident recovery.

Candidate commands include:

```text
recursion                         # attach to general interactive view
recursion attach head
recursion attach arm <id>
recursion attach task <id>
recursion workspace open [layout]
recursion view graph|events|system
recursion detach
recursion daemon status|start|stop|restart
recursion arm pause|stop|kill <id>
```

Names are provisional. The interactive command language, one-shot CLI and Telegram commands
must compile into the same authenticated command schema. Typing in a terminal does not gain
more authority than the active operator and GoalSpec.

## Persistence and termination

The daemon owns canonical state and actor leases. A managed process broker owns external CLI,
PTY/ConPTY and tool process groups. Each process records actor, task, route, worktree, start
epoch, parent process group, cancellation capabilities and recovery policy.

- Client disconnect or terminal close: detach only; persist the last acknowledged stream
  cursor and continue the actor under policy.
- Daemon restart: recover canonical state, reconcile process/remote observations, and never
  relaunch an ambiguous external effect blindly.
- Graceful stop: close admission, request a safe checkpoint, enforce a deadline, then report
  any process or effect that did not terminate.
- Force stop: use the platform process-tree primitive, revoke leases, quarantine uncertain
  outputs and preserve enough evidence to explain the interruption.
- Emergency stop: block new effects independently of whether an interactive client is open.
- Uninstall or service shutdown: must not leave unmanaged descendants; failure to prove process
  ownership is a visible blocker.

Persistence must remain lightweight. Detached output is bounded and stored by retention class;
slow or absent terminals cannot backpressure the canonical writer or cause unlimited buffers.

### Versioned client protocol

The requirements contract is ready to freeze around a daemon epoch and monotonically
increasing per-stream cursor. Wire encoding and numeric limits remain experimental.

```text
HELLO    protocol version, client/actor/stream, last epoch/cursor, dimensions, capabilities
WELCOME  epoch, stream, replay floor, snapshot cursor, input lease, negotiated capabilities
SNAPSHOT current terminal state and modes at one durable cursor
EVENT    epoch, cursor, class, actor/task/process identity, payload
ACK      last accepted cursor
CREDIT   bounded byte and event allowance
INPUT    idempotent command id, active input lease, raw bytes
RESIZE   idempotent command id, active resize lease, columns and rows
RESET    replay gap, current snapshot and new cursor
```

One active lease owns input and resize; other clients are passive readers. The process broker
drains PTY output even with no clients. Each client has bounded queues and explicit credit.
Presentation frames may be coalesced, but command, lease, process-exit, termination and
canonical state events may not be dropped. A slow client disconnects with its final cursor and
later receives replay or `RESET + GAP`; reconnect never relaunches a process.
The canonical event is appended before a presentation queue may coalesce or drop anything; a
renderer queue is never the durable log. Queue exhaustion must leave owner control and recovery
capacity available.

### Native process boundaries

- Windows uses ConPTY as the UTF-8/VT byte transport and a restrictively secured local IPC
  endpoint. A Job Object is the primary process-scope kill/accounting boundary, but breakaway
  and alternate creation paths are explicit negative fixtures. Microsoft documents that the
  pseudoconsole host is responsible for display and input; this does not make ConPTY the actor
  or process owner.
- Linux uses a PTY plus session/foreground process groups for terminal semantics, with a cgroup
  v2 or systemd scope as the stronger descendant and resource boundary and pidfds where
  available. `setsid()`, cgroup migration and privilege boundaries are negative fixtures.
- SSH, tmux, Zellij, Git Bash/MSYS2 and Neovim are clients or adapters. None is canonical
  process identity. Zellij resurrection may rerun commands, and Neovim session restoration may
  restart terminal jobs; both behaviors must be distinguished from broker reconnection.

The platform evidence supports these primitives, not the unqualified claim that every child
can be contained. â€œFull-tree terminationâ€ is an acceptance result obtained from the supported
OS matrix.

## Configuration in the terminal

The TUI renders from the same versioned configuration schema as files and one-shot commands.
It shows effective value, provenance, allowed values, affected actors, application lifecycle
(`live`, `checkpoint-applied`, `restart-required` or new GoalSpec) and a before/after diff.

Interactive editing never makes terminal widget state canonical. An accepted change is an
authenticated command and event. Concurrent terminals either observe the committed new version
or receive a typed stale-version conflict; last-keypress-wins behavior is forbidden for policy.

Saved terminal layouts contain view and presentation preferences, not credentials, authority or
actor state. Layout configuration may be local/private and portable separately from runtime
configuration.

## Non-flicker and quality requirements

The interactive CLI should be calmer and more legible than common repaint-heavy agent CLIs:

- update only changed cells/regions rather than clear and redraw the whole terminal;
- use a stable viewport, explicit scrolling and bounded/coalesced progress events;
- preserve typed input while background events arrive;
- prevent interleaved worker output from corrupting prompts or each other;
- render stable actor/route/task labels and make unseen activity explicit;
- handle resize, Unicode width, color capability, reconnect and high-output bursts;
- respect reduced-motion/color/accessibility settings and provide a plain-text mode;
- keep canonical logs complete even when live presentation samples repetitive deltas;
- measure input latency, frame time, CPU, wakeups, memory and dropped/coalesced events.

Animation and decoration are subordinate to fast comprehension. Streaming reasoning is not
required, and hidden chain of thought is never treated as a display or recovery dependency.

## Communication visualization recommendation

Use two coordinated representations rather than forcing everything into one transcript:

1. a stable actor/work graph showing Head, ARMs, children, leases and typed in-flight requests;
2. a selected actor or correlation stream showing conversation, command, evidence and status
   events in causal order.

Color alone must not encode actor or state. Cross-window selections can share a durable view
cursor, but view cursors never affect execution. A shared multi-ARM room presents each speaker's
logical identity and GoalSpec; it does not merge their private contexts.

## Research requirements

The v3.0 portability gate covers native Windows and Linux terminal stacks. The exact initial
application matrix must be chosen from measured behavior, but it must include the Windows
ConPTY/Job Object path and the Linux PTY/session/process-group path with at least one maintained
native terminal on each OS. Compatibility breadth beyond that behavioral floor may ship later.

Git Bash is a distinct Windows/MSYS2 shell-and-PTY translation case, and Neovim is an embedded
terminal/editor client rather than an operating-system terminal. Both are high-priority research
targets, along with other popular terminals, but must pass attach/detach, input, resize, process
ownership and plain-text tests before being listed as supported. Popularity does not justify a
compatibility claim that leaves work orphaned or input corrupted.

Before freezing the contract, compare primary specifications and implementations for:

- Windows ConPTY, Job Objects, process groups and Windows Terminal control/integration;
- POSIX PTYs, sessions/process groups/signals and common service supervisors;
- tmux, Zellij and detachable-session persistence models;
- mature terminal UI diff renderers, event loops, accessibility and resize behavior;
- agent CLIs' transcript, streaming, subprocess, resume and multi-session failure modes;
- IPC authentication, stream cursors, flow control and bounded buffering;
- terminal layout portability without making an OS terminal application mandatory;
- Git Bash/MSYS2 translation and Neovim embedded-terminal behavior, plus a documented path for
  adding later compatibility profiles without changing canonical actor/process semantics.

Required experiments include attach/detach storms, terminal crashes, daemon restart, orphan and
grandchild processes, high-output workers, resize/Unicode tests, slow clients, simultaneous
configuration edits, PAUSE/kill races, Windows/Linux parity, resource soak and a screen-reader
plain-text workflow. The acceptance decision must include failure evidence, not only a polished
demo.

The minimum `PERF-010` matrix uses 0, 1, 5, 10 and 20 passive views under idle and high-output
loads. It records p50/p95/p99 input and frame latency, CPU, RSS, wakeups, IPC bytes, queue
high-water marks, coalesced presentation frames, process/handle counts and canonical append
delay. The contract fails if views multiply model/worker calls, block canonical writes, cause
unbounded memory, corrupt input, lose durable events or turn detach/reconnect into relaunch.

Primary platform anchors opened 2026-09-03 are Microsoft
[pseudoconsole](https://learn.microsoft.com/en-us/windows/console/pseudoconsoles) and
[Job Object](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
documentation, Linux kernel [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html),
POSIX.1-2024 Issue 8 [terminal](https://pubs.opengroup.org/onlinepubs/9799919799/basedefs/V1_chap11.html) and
[`kill`](https://pubs.opengroup.org/onlinepubs/9799919799/functions/kill.html) specifications,
and [SSH connection protocol](https://www.rfc-editor.org/rfc/rfc4254.html). Their applicability
is limited by the declared platform version and actual isolation available to the broker.

## Logical Stop in the integrated finite recovery domain

FRI-003 preserves Stop admission and application across full finite pipelines, blocked pumps,
bounded partial frames and pending RESET. Each cursor profile has 32,500 pending-Stop cyclic
components, all excluded by named weak fairness of stop_apply; without that fairness they remain
counterexamples to eventual logical Stop. No data-delivery fairness is assumed or proved.

This variable is a logical control receipt, not execution fencing: offers, pumping and emission
can still occur after control=2. A framing fault quarantines new source offers only; represented
candidates and received protocol messages may continue. No actual oversize parser rejection or
physical termination is implemented by these models. Queue bytes are independently stressed,
but the two-event limit is not independently saturated with the current mixed costs and byte
capacity2. Preserve the physical at-birth containment result as separate evidence, and retain
all ten terminal controls, three Stop modes, forks, persistent assistants and remote recovery.
See [scope and evidence](construction/flow-recovery-integration-20260907/evidence.json).

## Receiver installation evidence, 2026-09-07

RSE-003 separates wire audit recovery from committed application recovery. The local receiver stores content, stream/epoch, last installed snapshot generation and both cursors in one replacement envelope. Kills after RESET emission or staging leave application A/cursor1 despite accepted-reset in the unchanged wire oracle. A kill after envelope publication leaves AÃ©/cursor2. Fresh readers and receivers complete to AÃ©B/cursor3; a repeated attachment appends nothing. Application generation means the last installed snapshot, so no-reset replay can consume a newer selected store generation without changing that field.

These are named process-cut observations, not power-loss durability or exactly-once delivery for arbitrary external effects. The [probe dossier](construction/replay-store-evidence-20260907/dossier.md) retains all literal wire traces and application envelopes. Independent control lanes, all ten terminal controls, three Stop modes, forks, persistent assistants and remote recovery budgets remain required; helper termination here does not implement product Stop or node fencing.

## Generation pin reclamation evidence, 2026-09-07

GRA-003 observes reclamation only after a generation is noncurrent and has no durable pins. Persist retirement intent before deleting explicitly enumerated old files; restart resumes a partially deleted old generation. Current-generation pins do not unnecessarily block old-generation cleanup. Reader completion/death release requires the trusted controller to wait the exact owned child; timeout alone never removes a pin. A committed pin whose reply was lost survives restart and can be explicitly cancelled without pretending a reader died.

This is a synthetic store/reader fixture, not deployed terminal or node recovery. It preserves all ten terminal controls, three Stop modes, persistent assistants, preserved forks and relative recovery budgets. Binding remote identity, physical Stop and authenticated recovery remain unresolved. [Named schedules](construction/generation-authority-20260907/dossier.md).

## Generation and effect composition, 2026-09-08

GCM-003 checks a previously admitted permit separately from local task revocation and recipient fence installation. Execution before installation remains visible exposure; an old permit after installation refuses. Authenticated-operation receipt identity is an abstract environment predicate with wrong-operation/payload/auth negative controls. A late valid receipt can classify the original effect while another incarnation still needs its own completion; old transport ACK cannot substitute. Fork inspection retains content without restoring its former grant. This is task Stop, not PAUSE, and supplies no permission to bypass literal M1A E-PAUSED. Full wire joins, receiver relocation and live integration remain open. [Finite relations](construction/composition-model-20260908/specification.md).

## PAUSE and retained effect evidence, 2026-09-08

EEJ-002 observes the literal running/PAUSE pair through the immutable M1A kernel. The same otherwise-valid observation payload accepts while running and refuses `E-PAUSED` after an admitted current-immediate pause. Refusal emits no event and leaves the canonical projection unchanged; its separate receipt journal grows. An exact duplicate accepted before PAUSE returns the original receipt without an event because duplicate handling precedes current admission. Do not describe that as fresh admission.

M1C can classify retained historical evidence while current context is paused; its trace has no current PAUSE-state field. Preserve the original admission's running state and carry current PAUSE outside the trace. Actual M1A ambiguity/quarantine persists after a later admitted success observation. A runtime bridge needs an explicit canonical resolution protocol; an adapter, terminal reconnect or historical classification cannot silently clear it. Three Stop modes, all ten terminal controls and preserved forks remain unchanged. Literal controls and limits.

## Shared snapshot installation boundary â€” 2026-09-08

SCM-003 distinguishes captured, copied, advertised, emitted and installed content.
Restart after recovery emission cannot imply application installation. Installation
preserves newer broker-owned source entries; an ordered finite witness retains
a prequeued newer entry through installation, grants fresh credit and delivers it
while current evidence hold and quarantine remain intact. Stop applies from a
saturated presentation queue through its independently enabled control path.

The experiment queue is broker-side undelivered source ownership. It is not the
M1B receiver pending/unacknowledged queue. M1B-WIRE-PROFILE still requires a gap or
exhaustion before RESET, clears receiver credit/pending state, and requires fresh
CREDIT before later EVENT. The new internal model incorporates gap/credit limits
but does not prove complete wire, byte-credit, ACK, framing or restart-handshake
correspondence. Internal phase actions must not be advertised as observed upstream
integration. All ten terminal controls, three Stop modes, preserved forks and
persistent assistants remain required.

See [shared-model evidence](construction/shared-composition-20260908/evidence.json)
and the literal-wire follow-up in
[next probes](construction/shared-composition-20260908/next-probes.md).

## Presentation and reservation separation â€” 2026-09-08

The SC-WR-01 finite construction installs an old copied view while a newer durable
reservation remains held. Installed presentation cursor and envelope replay cursor
are separate; replay observes the intact authoritative revision and grants no
allowance. Reader-pin completion, local worker exit/wait and quota reset likewise
preserve uncertain external effects. This does not implement the full M1B wire,
queue/credit, framing or ACK correspondence. SC-WIRE-02 remains next, followed by
a coupled model before aggregate promotion. All ten controls, three Stop modes,
forks and persistent assistants remain required. [Evidence](construction/shared-reservations-20260908/findings.md).

## SC-WIRE-02 â€” 2026-09-08

WIR-001 distinguishes source future entries, receiver pending entries, immutable snapshot content and installed snapshot cursor. In16 finite configurations, GAP via WELCOME requires RESET, fresh CREDIT and independent application installation before EVENT. Failed ACK holds pending; successful ACK creates no credit. Fixed-epoch receiver restart preserves ownership. Literal M1B cannot prove application installation. Full prior flow/ingress bounds remain separately checked, not imported into this narrow product.
[Evidence and limits](construction/wire-correspondence-20260908/evidence.json).

## SC-EPOCH-03 â€” 2026-09-08

EPC-002 models the Head/consultation/executor wait cycle, exposes a dependency blocker and releases the sole eligible executor on suspension. Stop of borrowedA retires its alias and continuation while unrelatedB progresses on its separately admitted capacity. Alias retirement is independent of numeric generation: assigning an already-current serial cannot keep delivery live. Cancellation delivery leaves remote outcome unknown and the inherited six-unit reservation held.
[Evidence and residuals](construction/typed-consultation-20260908/evidence.json).

## SC-COUPLED-01 â€” 2026-09-08

CPL-003 retains four-event costs1,2,1,2 and the full declared stage, in-flight, receiver and frame bounds. Literal M1B acceptance is checked separately from application installation, catalogue binding, ingestion and effect authority. The no-gap witness emits all four events; gap witnesses retain explicit delivered/covered distinctions. Synthetic framing and intact requester restart do not prove physical receiver recovery.
[Evidence and limits](construction/coupled-construction-20260908/handoff.md).

## Coupled evidence cut and PAUSE â€” 2026-09-08

Supplemental retained-cut delivery stays separate from the full four-event stream. ACK/restart/snapshot installation cannot give a stale, foreign or uncertain cut reservation-settlement authority.
See [scoped findings and residuals](construction/coupled-cut-pause-20260908/findings.md). No runtime integration or full formal freeze follows.

## Retained-cut publication â€” 2026-09-08

Retained-cut historical installation may expose a pure joined classification under PAUSE
without changing current selected revision, source incarnation/head, runtime admission or
external-effect allowance. Exact internal publication receipt lookup is awareness. Current
reconciliation still requires the selected cut plus all inherited literal admission guards.
No runtime service, operational PAUSE behavior or common-writer bridge is implemented.

See [SC-CUT-PUB-01](construction/cut-publication-20260908/handoff.md) for evidence and limits.

## Source collection and retained readers â€” 2026-09-08

Complete captured inventory and current reconciliation authority remain separate. The
source experiment retains copied historical cuts, owned wait/release, typed consultation
evidence and the full four-event stream. It adds no service, runtime migration, physical
recovery or orphan-reader reclamation mechanism.
See [continuation](construction/cut-source-overnight-20260908/handoff.md).
