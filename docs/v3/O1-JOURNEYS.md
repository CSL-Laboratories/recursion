# O1 Journey Catalog and Crosswalk Specification

**Version:** `o1-journeys v0.1-draft`<br>
**Status:** DRAFT proposal under envelope G; nothing accepted until a rule-H receipt<br>
**Parent Contract:** [O1-CONTRACT.md](O1-CONTRACT.md)<br>
**Parent Issue:** [CSL-205](https://linear.app/csl-laboratories/issue/CSL-205) (Outcome O1)<br>
**Inherited Decisions:** [D-024](../../state/DECISIONS.md#d-024--frontier-claim-productive-continuity), [D-022](../../state/DECISIONS.md#d-022--one-pausestop-vocabulary-across-config-formal-model-and-broker-g-07), [D-036](../../state/DECISIONS.md#d-036--mobile-offline-retention-online-only-private-view-o-01), [D-038](../../state/DECISIONS.md#d-038--d-034d-019-supersede-older-first-release-guimobile-exclusions-o-03), [OR-021–OR-039](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07)

---

## 1. Executive Summary and Architecture

Per decision [D-024](../../state/DECISIONS.md#d-024--frontier-claim-productive-continuity), Recursion's core technical differentiator is **productive continuity** — the guarantee that an authorized task survives a harness switch, context compaction, source code fork, or machine loss while preserving authority, evidence, and honest accounting.

This catalog formalizes the six primary demonstration journeys (J1 through J6) and maps them across three execution modes:
- **Native-Only Mode:** Driven exclusively by Recursion's internal model-and-tool loop and local execution kernel.
- **Upstream-Only Mode:** Driven through an external installed harness CLI (e.g., Claude Code, Codex, Grok, OpenCode) utilizing the user's existing authenticated subscription, without intermediating credentials or requiring hidden API keys.
- **Combined Mode:** Orchestrated by Recursion Head and ARM executives, delegating bounded implementation turns to upstream harnesses while retaining canonical state, context compaction, and verification in Recursion.

---

## 2. Catalog of the Six Demonstration Journeys (D-024)

### Journey J1: Native → Eligible Upstream → Native Continuation
**Headline:** Multi-harness task continuity without context loss or credential leaks.

- **Actor & Start State:** Developer (e.g., Lucas) starts with a clean git branch on a local workspace and submits a feature request via the CLI.
- **Useful Endpoint:** A verified, reviewable git diff with complete execution evidence, passing test gates, and unified token/cost accounting.
- **Workflow Steps:**
  1. *Goal Formulation:* Recursion Head ingests developer intent and compiles an immutable GoalSpec.
  2. *Native Scaffolding:* A native ARM executive initializes the task, sets up the workspace, and executes preliminary static analysis.
  3. *Delegated Upstream Execution:* ARM delegates a complex refactoring turn to an eligible upstream harness (e.g., Claude Code via `claude -p` stream-json or Codex App Server via stdio).
  4. *Stream Ingestion:* Recursion process broker monitors the upstream event stream, enforcing pre-call budgets and filtering sensitive tokens.
  5. *Native Re-Adoption:* Upstream finishes; the native kernel re-adopts workspace state, updates the execution ledger, runs local verification tests, and formats a unified diff.
- **Fault & Recovery Case:** Upstream harness process abruptly crashes, times out, or encounters a 429 rate limit mid-turn. The native broker catches the error, records the event in `cooldowns.json`, preserves dirty in-progress work intact without destructive rollback, and checkpoints task state for developer steering or class-consistent failover (CSL-83).
- **Execution Modes:**
  - *Native-Only:* All planning, code editing, and test runs execute via local models or direct BYOK provider endpoints without invoking external CLIs.
  - *Upstream-Only:* The entire task from goal prompt to diff generation executes inside the upstream harness binary. **Dependency:** Requires an installed, authenticated CLI on `$PATH` (`claude`, `codex`, `grok`, etc.) with an active user login. No separate API keys or secret intermediation permitted. Planning and verification in this mode use that CLI and local checks. They do not depend on a provider API or a model.
  - *Combined:* Native Head owns GoalSpec, budgets, and verification; upstream executes code turns; native verifies output.

---

### Journey J2: Compaction After Upstream-Session Loss
**Headline:** Context survival and non-lossy intent recovery across session crashes.

- **Actor & Start State:** Developer running an extended, multi-hour autonomous development workflow. Upstream session has accumulated 150k+ tokens of scratchpad and tool output.
- **Useful Endpoint:** Continuous task execution across a fresh session boundary without loss of parent goals, constraints, or completed milestones.
- **Workflow Steps:**
  1. *Context Growth:* Upstream agent executes multiple tool calls, rapidly exhausting available context window.
  2. *Session Disruption:* Upstream harness process crashes due to out-of-memory or API gateway session reset.
  3. *Compaction Trigger:* Recursion deterministic context governor detects session loss and initiates hybrid adaptive compaction (opt-in per D-019).
  4. *Capsule Synthesis:* The governor distills raw session logs into a content-addressed Context Capsule: preserves non-lossy OwnerIntent, immutable GoalSpec, active constraints, file mod lists, and test evidence; strips redundant tool chatter.
  5. *Rehydration:* A new upstream session (or native loop) is spawned and primed with the Context Capsule.
  6. *Resumption:* Execution resumes seamlessly from the exact unblocked task node.
- **Fault & Recovery Case:** Compactor accidentally omits an essential negative constraint from the original prompt during distillation. The contract falsifier compares the capsule constraint manifest against the root GoalSpec hash; finding a discrepancy, it halts execution and requests human confirmation before allowing code mutations.
- **Execution Modes:**
  - *Native-Only:* Native context governor compacts local conversation buffers directly into local storage engine (technology unselected; SQLite is an unrun challenger per D-028/PRODUCT.md) and resumes the native loop.
  - *Upstream-Only:* Compacted state is synthesized into a single structured prompt file (`--prompt-file`) for the newly spawned CLI session. **Dependency:** Upstream CLI must support non-interactive file-based prompt ingestion. Compaction does not depend on a provider API or a model.
  - *Combined:* Native governor distills upstream history into a capsule and injects it into a fresh upstream or native instance.

---

### Journey J3: Full Stop with Unreachable Work
**Headline:** Halting authority, dispatch fencing, observed local termination, and recipient enforcement across distributed workers without claiming unverifiable global quiescence.

- **Actor & Start State:** System is executing high-concurrency tasks with multiple background subagents, worker threads, and remote execution nodes.
- **Useful Endpoint:** Observed local termination of managed child processes, dispatch fencing (monotonic fence increment invalidating future writes/leases), recipient enforcement by remaining nodes, and explicit quarantine reporting preserving unresolved remote effects without claiming an unverified global halt.
- **Workflow Steps:**
  1. *Stop Command:* Developer or automated watchdog issues `stop --mode full` per [D-022](../../state/DECISIONS.md#d-022--one-pausestop-vocabulary-across-config-formal-model-and-broker-g-07).
  2. *Observed Local Termination:* Process broker broadcasts immediate signals (SIGTERM/SIGKILL) to observed local child processes, background watchers, and subagent shells, verifying process exit.
  3. *Unreachable Node Handling:* A remote execution node is partitioned or unresponsive. The broker records the endpoint as partitioned/quarantined, preserving unresolved external effects.
  4. *Dispatch Fencing:* Monotonic fencing token is bumped in the canonical store, permanently invalidating the remote node's write authority and expiring its execution lease.
  5. *Recipient Enforcement:* Reachable worker nodes and store backends enforce the updated fence epoch, rejecting any delayed or buffered writes carrying superseded tokens.
  6. *State Settlement & Audit:* Local workspace locks are released; in-flight partial edits from unreachable tasks are preserved in a quarantined scratchpad; system emits an explicit audit summary distinguishing observed locally terminated processes from quarantined/unreachable endpoints with unresolved remote outcomes.
- **Fault & Recovery Case:** The partitioned remote node recovers 30 seconds later and attempts to flush its cached git commits to the shared workspace. The kernel's monotonic fence rejects the commit token; writes are blocked, and an alert is logged.
- **Execution Modes:**
  - *Native-Only:* Kills local native execution threads, cancels async runtimes, and fences local file locks.
  - *Upstream-Only:* Terminates the parent upstream CLI process group (`kill -TERM -PGRP`); inspects OS process table to verify spawned child compilers/linters are dead. **Dependency:** OS process group supervision tools.
  - *Combined:* Terminates local upstream wrappers and issues network fence commands to remote worker nodes.

---

### Journey J4: Dirty-Work Fork
**Headline:** Safe autonomous intervention in non-clean developer workspaces, capturing and preserving both fork versions.

- **Actor & Start State:** Developer invokes Recursion inside a repository containing untracked files, unstaged modifications, and half-written code.
- **Useful Endpoint:** Task completes successfully in isolation without clobbering developer edits, capturing and preserving both fork versions (developer's dirty work and isolated task branch) with a clean, conflict-free integration path.
- **Workflow Steps:**
  1. *Source Preflight:* Workspace manager inspects working tree, detecting dirty uncommitted modifications.
  2. *Notice & Two-Version Capture:* Per [OR-028](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07), system alerts the developer, preserves the developer's original dirty working tree intact, and creates an isolated Git worktree or snapshot branch (`csl-<N>-fork`). Both fork versions are recorded and preserved.
  3. *Isolated Execution:* Autonomous agent executes strictly within the isolated worktree, referencing dirty files in read-only mode if necessary.
  4. *Candidate Completion:* Agent finishes task, verifies changes against test gates, and commits to the fork branch.
  5. *Disclosure & Side-by-Side Review:* System presents a side-by-side reviewable diff comparing the developer's original dirty state, the agent's fork changes, and the base commit.
  6. *Reconciliation:* Developer selects integration strategy: apply agent changes via patch, rebase onto fork, or discard.
- **Fault & Recovery Case:** Autonomous work modifies a file that the developer simultaneously edited in their working copy. The automated integration refuses to force-apply changes; it preserves both the developer's working copy and the fork branch intact, generating a 3-way conflict report for manual resolution.
- **Execution Modes:**
  - *Native-Only:* Native harness manages Git worktrees directly via local filesystem operations.
  - *Upstream-Only:* Invokes upstream CLI with its working directory explicitly pinned to the isolated worktree directory (`--cwd <worktree_path>`). **Dependency:** Upstream CLI must respect working directory isolation without reading parent directory uncommitted state.
  - *Combined:* Native harness creates worktree; upstream executes turns inside it; native manages git merge review.

---

### Journey J5: Node/Client Loss and Cross-Platform Reconnection
**Headline:** Seamless operational migration between Windows, Linux, and mobile control surfaces with reconciliation of uncertain in-flight effects before replacement execution.

- **Actor & Start State:** Long-running autonomous task started on a Linux coordinator server via interactive terminal; user closes laptop and disconnects.
- **Useful Endpoint:** Reconnection and live steering resumed from a different device (e.g., Windows workstation or mobile client) without task interruption or duplicate execution.
- **Workflow Steps:**
  1. *Session Detachment:* User closes the terminal window. Per [OR-012](PRODUCT.md#primary-workflows), the coordinator daemon owns process lifetime; detachment does not interrupt work.
  2. *Continuous Execution:* Daemon persists execution events to the local storage engine on local ext4/NTFS per [D-028](../../state/DECISIONS.md#d-028--platform-floors-closes-g-11) (specific store unselected).
  3. *Reconnection:* User opens Recursion CLI on Windows or an authenticated mobile view.
  4. *Handshake & Replay:* Client performs zero-token authenticated handshake. Coordinator streams event backlog from the last acknowledged cursor.
  5. *Live Attachment:* Client rehydrates current progress, active ARM topic, and resource metrics.
  6. *Interactive Steering:* Developer injects a steer directive (`steer "focus on unit tests first"`); coordinator routes it immediately to the active ARM.
- **Fault & Recovery Case:** Coordinator machine suffers an abrupt VM reset / power loss, or a remote worker node is lost mid-turn. Upon restart/reconnection, the system must reconcile uncertain in-flight effects (verifying whether external tool calls, remote commits, or partial execution took effect) before dispatching replacement execution, ensuring idempotency and preventing duplicate mutations ([D-020](../../state/DECISIONS.md#d-020--availability-contract-automatic-node-takeover-required-core-is-single-node-closes-g-06), [D-028](../../state/DECISIONS.md#d-028--platform-floors-closes-g-11)).
- **Execution Modes:**
  - *Native-Only:* Native daemon hosts the event broker, local state store, and RPC socket.
  - *Upstream-Only:* Native broker retains process stdout/stderr streams from the background upstream harness; reconnecting client reads live log stream. **Dependency:** Coordinator host daemon must wrap upstream process stdio.
  - *Combined:* Full native state and upstream process telemetry served to multi-platform clients.

---

### Journey J6: Honest Subscription / Quality Tradeoff
**Headline:** Deterministic multi-route optimization with truthful quota, cumulative recovery accounting, and quality reporting.

- **Actor & Start State:** Developer running high-volume task with access to an active Anthropic subscription, OpenAI API key (BYOK), and a local Ollama model.
- **Useful Endpoint:** Task completed within explicit user-defined resource constraints, accompanied by an audited accounting ledger with cumulative recovery reservations and zero falsified counters.
- **Workflow Steps:**
  1. *Optimization Profile:* Developer selects optimization ranking: responsiveness first, subscription efficiency second, task completion third ([OR-024](PRODUCT.md#optimization-target)).
  2. *Weighted Dispatch:* Turn requests route to the primary subscription route per `routing.weights` ([CSL-83](harness/CSL-83.md)).
  3. *Pre-Call Metering:* Governor inspects remaining allowance before call. If token ceiling is exceeded or rate-limit reported, call is refused ([CSL-83](harness/CSL-83.md)).
  4. *Class-Consistent Fallback:* Cooldown timestamp recorded in `cooldowns.json`. System executes fallback to next enabled route of identical class (live to live, offline to offline).
  5. *Truthful Accounting & Cumulative Recovery:* Token usage reported by providers is recorded. Missing counters are stored explicitly as `null` / `"unknown"`, never coerced to `0`. Recovery operations track cumulative allowances per OR-032.
  6. *Pareto Audit:* Final report displays verified test quality alongside exact tokens, estimated cost, and latency breakdown.
- **Fault & Recovery Case:** All configured live subscription routes exhaust quota. System refuses to silently switch to a metered commercial API key without explicit human authority ([OR-001](DECISION-REGISTER.md#owner-rulings-and-refinements--2026-09-03), [D-021](../../state/DECISIONS.md#d-021--delegation-over-credential-intermediation)); it checkpoints the task and pauses cleanly.
- **Execution Modes:**
  - *Native-Only:* Defined by Recursion's internal ownership of the model-and-tool loop without delegating orchestration to external CLIs; routes through eligible direct BYOK provider endpoints (e.g. OpenAI/Anthropic APIs) or local model endpoints without selecting or requiring a specific model server; tracks RAM/VRAM and truthful token usage.
  - *Upstream-Only:* Dispatches turns through installed subscription CLIs; respects CLI rate limits without falling back to raw API keys. **Dependency:** CLI must report honest exit codes or token usage metadata.
  - *Combined:* Balances native reasoning with upstream tool executions, consolidating accounting into a single lineage ledger.

---

## 3. Explicit Journey × Surface × Control Coverage Matrix

Per [OR-028](PRODUCT.md#mark-2-capability-map-â-research-synthesis-2026-09-07), [D-022](../../state/DECISIONS.md#d-022--one-pausestop-vocabulary-across-config-formal-model-and-broker-g-07), [D-036](../../state/DECISIONS.md#d-036--mobile-offline-retention-online-only-private-view-o-01), and [D-038](../../state/DECISIONS.md#d-038--d-034d-019-supersede-older-first-release-guimobile-exclusions-o-03), the four foundational control actions (`approve`, `stop`, `steer`, `resume`) and offline retention must be explicitly evaluated across all client surfaces for every journey (J1 through J6).

> [!IMPORTANT]
> **Mobile Client Coverage Requirements (Two Distinct Gates):**<br>
> Per the owner plan, mobile coverage is enforced at two distinct structural layers:<br>
> 1. **Surface Control Matrix Cells (`O1E-002`):** Every journey must have explicit control-point cells for the Mobile column using the controlled vocabulary below.<br>
> 2. **Mobile Capability-Row Mapping (`O1E-016`):** Every journey J1 through J6 must also be explicitly mapped in the `"journeys"` array of mobile client capability row `CAP-016` in `docs/v3/O1-CAPABILITY-MATRIX.json`. Omitting any journey from `CAP-016` violates journey-to-mobile-row coverage even if the surface control cells below are populated.

> [!IMPORTANT]
> **Controlled Vocabulary:** Cell values in the matrix below are restricted strictly to:
> - `offered-proposed`: Capability is proposed and designed for this journey and surface.
> - `not-offered`: Explicitly not offered or prohibited on this surface (e.g. offline protected redisplay under D-036).
> - `unknown`: Unresolved pending empirical evidence or founder decision (e.g. mobile Full Stop remote process supervision is unmeasured/unapproved, NOT attributed to D-036).

| Journey | Control Point | CLI (Canonical) | TUI (Terminal Multiplexed) | GUI (Built, Opt-In) | Mobile (Online-Only) | Coverage Rationale & Notes |
|---|---|---|---|---|---|---|
| **J1** | **Approve** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | GoalSpec approval and tool confirmation via authenticated online view. |
| **J1** | **Stop (Aggressive)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Forceful process termination via local signal or remote coordinator RPC. |
| **J1** | **Stop (Full)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `unknown` | Remote process tree supervision from mobile is unproved/unresolved. |
| **J1** | **Stop (Lazy)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Checkpoint halt flag set in state store upon turn completion. |
| **J1** | **Steer** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Directive injection routed to active ARM executive topic. |
| **J1** | **Resume** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Re-attach to paused run with fresh credential check. |
| **J1** | **Offline Redisplay** | `not-offered` | `not-offered` | `not-offered` | `not-offered` | Protected offline content redisplay forbidden by D-036 across all clients. |
| **J2** | **Approve** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Approval of rehydrated Context Capsule and continuation plan. |
| **J2** | **Stop (Aggressive)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Halt compaction or rehydration immediately. |
| **J2** | **Stop (Full)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `unknown` | Remote process group termination from mobile unmeasured. |
| **J2** | **Stop (Lazy)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Halt after current capsule synthesis commit boundary. |
| **J2** | **Steer** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Inject constraints or adjust compaction priorities. |
| **J2** | **Resume** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Resume session rehydration from synthesized Context Capsule. |
| **J2** | **Offline Redisplay** | `not-offered` | `not-offered` | `not-offered` | `not-offered` | D-036 online-only rule applies to all capsule metadata. |
| **J3** | **Approve** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Approve quarantine status or post-stop cleanup plan. |
| **J3** | **Stop (Aggressive)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Primary trigger for immediate local process tree termination. |
| **J3** | **Stop (Full)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `unknown` | Mobile dispatch of full-shell halt unverified; remote effects stay open. |
| **J3** | **Stop (Lazy)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Orderly checkpoint halt before fence revocation. |
| **J3** | **Steer** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Direct quarantine routing or selective worker restart. |
| **J3** | **Resume** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Resume fenced tasks after monotonic epoch increment and node recovery. |
| **J3** | **Offline Redisplay** | `not-offered` | `not-offered` | `not-offered` | `not-offered` | Quarantine logs viewable strictly via authenticated online session. |
| **J4** | **Approve** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Approve fork worktree creation and candidate patch integration. |
| **J4** | **Stop (Aggressive)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Terminate fork subagent execution immediately. |
| **J4** | **Stop (Full)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `unknown` | Mobile supervisor capabilities for isolated worktrees unverified. |
| **J4** | **Stop (Lazy)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Allow current test run in worktree to finish before halting. |
| **J4** | **Steer** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Direct agent to adjust file edits or avoid specific dirty paths. |
| **J4** | **Resume** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Re-attach to unfinished fork worktree branch. |
| **J4** | **Offline Redisplay** | `not-offered` | `not-offered` | `not-offered` | `not-offered` | Worktree diffs require active authenticated connection. |
| **J5** | **Approve** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Approve state recovery plan after worker loss or abrupt host reset. |
| **J5** | **Stop (Aggressive)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Abort replay or halt coordinator immediately. |
| **J5** | **Stop (Full)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `unknown` | Mobile full stop during host reconnection unmeasured/unverified. |
| **J5** | **Stop (Lazy)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Pause at next replayed commit boundary. |
| **J5** | **Steer** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Inject steering directive into reconnected ARM topic. |
| **J5** | **Resume** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Primary mechanism for client handover between Windows/Linux/mobile. |
| **J5** | **Offline Redisplay** | `not-offered` | `not-offered` | `not-offered` | `not-offered` | Zero offline private event redisplay permitted by D-036. |
| **J6** | **Approve** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Approve optimization profile ranking or class-consistent route fallback. |
| **J6** | **Stop (Aggressive)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Abort runaway turn immediately on token ceiling breach. |
| **J6** | **Stop (Full)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `unknown` | Mobile halt over multiple concurrent upstream runners unmeasured. |
| **J6** | **Stop (Lazy)** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Drain queue and pause before next metered dispatch. |
| **J6** | **Steer** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Adjust routing weights or throttle token allowances dynamically. |
| **J6** | **Resume** | `offered-proposed` | `offered-proposed` | `offered-proposed` | `offered-proposed` | Resume after cooldown expiration or budget replenishment. |
| **J6** | **Offline Redisplay** | `not-offered` | `not-offered` | `not-offered` | `not-offered` | Telemetry and cost ledgers viewable online only per D-036. |

---

## 4. Crosswalk of Scenarios X1–X5 to Journeys J1–J6

The five foundational scenarios specified across [ACCEPTANCE.md](ACCEPTANCE.md#first-usable-platform-release-â-accepted-2026-09-06) and [PRODUCT.md](PRODUCT.md#primary-workflows) map directly onto the D-024 journey architecture:

| Scenario ID | Scenario Name & Scope | Primary Journeys | Composed Interaction & Mapping |
|---|---|---|---|
| **X1** | **Website Preview**<br>([ACCEPTANCE.md](ACCEPTANCE.md#first-usable-platform-release-â-accepted-2026-09-06)) | **J1, J4, J6** | Web frontend task requiring local server spawn, asset compilation, and browser/preview verification. Uses J4 to isolate build artifacts, J1 for native/upstream tool turns, and J6 to monitor compilation resource bounds. |
| **X2** | **Bug Fix**<br>([ACCEPTANCE.md](ACCEPTANCE.md#first-usable-platform-release-â-accepted-2026-09-06)) | **J1, J4** | Reproduction of an issue via test, isolated fix implementation, test gate rerun, and clean reviewable diff generation. Maps directly to J1 (execution to diff) and J4 (clean tree isolation). |
| **X3** | **Interrupted Refactor**<br>([ACCEPTANCE.md](ACCEPTANCE.md#first-usable-platform-release-â-accepted-2026-09-06)) | **J2, J3, J5** | Broad multi-file refactoring interrupted by token exhaustion, power disruption, or user stop. Exercises J3 for clean halt, J2 for context compaction, and J5 for cross-platform state rehydration upon restart. |
| **X4** | **Eligible-Upstream-Only**<br>([ACCEPTANCE.md](ACCEPTANCE.md#first-usable-platform-release-â-accepted-2026-09-06)) | **J1, J6** | Entire workflow executed through an external CLI (e.g. Claude Code or Codex) without local model invocation or hidden API connections. Tests J1 upstream-only mode and J6 honest subscription accounting. |
| **X5** | **Questionnaire/Head Collaboration**<br>([PRODUCT.md](PRODUCT.md#primary-workflows)) | **J1, J5** | Interactive requirement refinement where an assistant consults Head across distinct sessions and nodes. Uses J5 for multi-session state synchronization and J1 for collaborative human steering. |

---

## 5. Specification Reconciliation: GUI & Mobile Packaging

> [!NOTE]
> **Governance Reconciliation Note:**<br>
> Early product documents (e.g., 2026-09-06 and 2026-09-07 drafts of [PRODUCT.md](PRODUCT.md#accepted-first-usable-platform-release-â-2026-09-06) and [DECISION-REGISTER.md](DECISION-REGISTER.md#owner-platform-clarification--2026-09-06) under OR-021) contained legacy phrasing stating `"terminal first, dedicated GUI later"`.<br>
> On 2026-10-02, founder decision [D-038](../../state/DECISIONS.md#d-038--d-034d-019-supersede-older-first-release-guimobile-exclusions-o-03) formally ruled that [D-034](../../state/DECISIONS.md#d-034--product-thesis-the-harness-that-scales-with-your-workflow) and [D-019](../../state/DECISIONS.md#d-019--release-model-build-comprehensively-ship-lean-install-opt-in-dot-option-b-rejected) **supersede** all older first-release GUI and mobile exclusions:
> - GUI and mobile clients are **in scope and built** as part of the comprehensive product surface.
> - They are packaged as **built-but-opt-in** clients over the core daemon, preserving a lean default terminal installation without deleting accepted capability.
>
> This document records this supersession as an architectural alignment without modifying protected governance records or deleting client specifications.
