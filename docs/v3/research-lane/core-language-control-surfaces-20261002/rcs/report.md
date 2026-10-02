# R-CS: Agent-harness control surfaces and orchestrators

Research date: 2026-10-02 UTC. Parent goal: `goal-recursion-v3-harness-orchestrator`. Recursion source baseline: `699a29d622d1a1d3cecdaf3504cc5d649fb9d20e` (PR 159). T3 Code anchor: v0.0.45, `6c8fed35dded9ff71c5b46807125457acbb76be6`.

## 1. Findings and decision boundary

**Finding [I]: Recursion's differentiation must be demonstrated as a combined continuity contract, not claimed from a feature checklist.** T3's documented transaction/receipt design and scoped authority are already substantial counterexamples to an assertion that other control surfaces are merely routers. Those are published design claims, not independently verified product behavior. [T03][T04]

**Finding [S]: T3 v0.0.45 was published on 2026-10-02 at 18:17:31 UTC.** Its release records all of the brief's named changes; the detailed evidence below separates release inclusion from what individual change descriptions establish. [T02]

**Finding [I]: The valuable comparison is where authority, unfinished effects, composition identity and accounting survive disruption together.** A product can resume a conversation without preserving those other properties. This study supplies falsifiable conceptual comparisons, not a verdict that Recursion or a peer already satisfies them.

**Authority [S]:** D-018/D-023/D-030/D-031/D-034/D-035 remain accepted. D-036 requires online-only private mobile views, reauthorization before private hydration, and no automatic queued-action replay. D-038 makes GUI/mobile built but opt-in, superseding older GUI-later entry prose. D-039 keeps prior stack recommendations advisory. D-022's Aggressive/Full/Lazy Stop vocabulary is unchanged. No product technology, legal position, route qualification, repository split or accepted decision is selected or reopened. [R01]

### Evidence vocabulary

- **S, source-documented:** a project's technical documentation, release, license or maintainers' change description says it. A maintainer's test claim remains S.
- **L, locally observed static:** this research inspected a pinned document or repository metadata. It does not mean software was executed.
- **I, inferred:** the analysis or conceptual consequence follows from named evidence but has not been established experimentally. Product-marketing assertions are explicitly labeled vendor claims, not validated capabilities.
- **U, unknown/unverified:** the selected public evidence does not settle the question. Unknown is neither absent, unsupported nor zero.

All public sources were retrieved on **2026-10-02 UTC**. Bracketed identifiers resolve to exact URLs, commit/release pins or dated mutable documentation in §10. Scope is documentation-only: no competitor product downloads, installation, execution, benchmark, provider/model request, account/store action, credential use or live host work. Reading public repository documents is not a runtime observation. No code, schemas, assets or proprietary designs are reproduced.

## 2. Category and landscape

### 2.1 Operational definition [I]

An **agent-harness control surface** lets an operator create, inspect or influence sessions executed by an identifiable harness, while exposing at least session identity and lifecycle. An **orchestrator** additionally coordinates session placement, concurrent work, delegation, recovery or approvals across execution contexts. A single product can be both. A model API router alone is excluded: selecting an endpoint does not establish ownership of a harness session. An editor with an external-agent protocol is adjacent prior art; the protocol is not itself an orchestrator.

The comparison uses one dated anchor, mobile-first remote wrappers, a self-hosted relay/hub, a worktree-oriented predecessor, and a commercial multi-harness surface. Native upstream control is retained as a baseline, not mislabeled provider-neutral. This is a purposive sample, not a census or market-share ranking.

### 2.2 Landscape inventory

| Project / comparison role | Harnesses and client surfaces | License / openness evidence |
|---|---|---|
| T3 Code v0.0.45, anchor | **S:** Codex, Claude Code, Cursor, Grok Build, OpenCode, Antigravity; local-server CLI, web, Electron desktop, iOS/Android clients. No distinct TUI established here. [T01] | **S:** repository MIT license; its scope does not settle upstream account terms. [T26] |
| Codex App Server, native baseline | **S:** Codex session API with resume/fork/steer/interrupt; not a multi-harness wrapper. **U:** client availability is not inferred from this API page. [U01] | **S:** inspected Codex source license is Apache-2.0; hosted/app terms are separate. [U03] |
| Claude Code Remote Control, native baseline | **S:** local Claude Code sessions reachable through browser and Claude mobile clients; remote device can stop running background tasks. [U07] | **S:** public repository license notice reserves rights and points to commercial terms; a public repository is not evidence of an open-source product license. [U11] |
| Agent Client Protocol v1, interface prior art | **S:** standardized client/agent sessions; load/resume capabilities negotiated separately. A protocol implementation's harness coverage must be verified per adapter. [U04] | **S:** protocol repository license is Apache-2.0. [U06] |
| Happy CLI 1.2.5, mobile-first wrapper | **S:** Claude Code/Codex plus documented CLI paths for Antigravity, OpenClaw and ACP agents; Gemini deprecated. CLI, web, iOS/Android; linked desktop stack is a separate baseline. [H2][H3] | **S:** MIT declaration and pinned license text. [H6] |
| HAPI v0.30.7, self-hosted hub | **S:** Claude, Codex, Cursor, Grok, Copilot, Kimi, OpenCode, DeepSeek Harness, Antigravity and Pi, with unequal adapter capabilities. [P2] **S:** CLI/upstream terminals, web/PWA, Telegram Mini App, native iOS/Android. [P3] | **S:** AGPL-3.0-only metadata and license text; no downstream compatibility conclusion. [P1][P7] |
| Vibe Kanban 0.1.44 snapshot, worktree predecessor | **S:** Claude, Codex, Gemini, Copilot, Amp, Cursor, OpenCode, Droid, CCR, Qwen; CLI-launched local/web workspace UI. Native mobile and dedicated orchestrator TUI **U**. [V3] | **S:** Apache-2.0. [V2] **S:** company shutdown/sunsetting announcement; later hosted operation **U**. [V8] |
| Conductor, commercial comparator | **S:** integrated Claude Code, Codex, Cursor and OpenCode. [C2] **I, vendor-claimed:** macOS, iOS, cloud and API; runtime availability untested. [C3] | **S:** proprietary service/client terms with separate component provisions; public product-source commit **U**. [C14] |

Peer pins: Happy `4beb3543cfdeb507be838016996f7a0f65cf7b49` (2026-09-22); HAPI `0239edf38e2da653d662f31039e24ccea04c7837` (2026-09-15); Vibe `4deb7eca8f381f7cbc1f9d15515a9ab8f8009053` (2026-04-24). Conductor's 2026-09-29 changelog page is headed 0.89.1 and includes 0.89.2/0.89.3 notes; this is not a verified installed/latest-binary pin. [H1][P1][V1][C1]


### 2.3 Architecture patterns and consequences [I]

| Pattern | What it simplifies | What remains difficult / failure mode | Evidence anchors |
|---|---|---|---|
| Per-machine execution owner, detachable clients | Filesystem/process ownership is explicit; clients can be replaced without relocating work | Offline owner remains unavailable; UI connection state cannot establish process fate; account identity must survive aggregation | T3 environment boundary [T03]; native remote baseline [U07] |
| Hub/relay with machine-local executors | Remote discovery, notifications and one operator view | Relay identity versus execution authority; lost/duplicated notifications; ownership handoff under partitions | T3 environment/relay separation [T04]; peer evidence below |
| Worktree/task orchestration | Parallel changes and review context can be separated | A worktree is not a sandbox; process termination is not external-effect rollback; archival can leave unresolved work | Vibe/Conductor evidence below; D-022 [R01] |
| Protocol-normalizing adapter boundary | Clients need fewer provider-specific concepts | A common vocabulary can erase native permission IDs, async-question lifetime or unsupported rollback semantics | T3 adapter constraints [T14]; ACP capability rules [U04] |

These are conceptual tradeoffs, not claims that a topology necessarily achieves security, availability or cheaper operation. A peer with a stronger documented contract is a stronger baseline, not an inconvenient outlier.

## 3. T3 Code deep dive

### 3.1 Architecture, state, credentials and authority

**S:** T3's architecture places workspace execution in its owning environment. Clients use authenticated RPC. A pure decider records intent; events, projections and durable command receipts commit together. Side effects occur afterward; an acknowledgement establishes committed intent, not finished provider work. Turn completion and checkpoint settlement are separate. [T03]

**S:** T3's service guidance keeps transport handlers thin and exposes capabilities through domain services usable by RPC, MCP, scheduled tasks and CLI. **I:** this is a useful cross-surface consistency pattern, not evidence that choosing its framework would produce Recursion's invariants. [T05]

**S:** Environment-issued sessions carry scopes; relay credentials are not environment login. Successful socket authentication does not authorize every RPC. The documented filesystem boundary is the environment account, not the project: read scope can cover files outside a workspace. **U:** this study establishes no independent security result. [T04]

**S:** T3 distinguishes a driver from a configured instance, documents instance-local state and provider-specific credential arrangements, and warns that opening a provider session may launch hooks, MCP servers or authentication. **I:** neither a local subprocess nor a health-check label proves credential non-access or inertness. Recursion's stronger D-029 launch-boundary obligation therefore cannot be discharged by analogy. [T14][R01]

### 3.2 Plugin and skill lifecycle

**S:** PR #14542 stops the thread's provider process, retains its conversation and starts a fresh resuming process on the next message. It also forces folder discovery and invalidates relevant menu caches; stop/refresh errors are displayed. This specific change is **restart-based agent-session refresh**, not hot replacement of a running agent. A menu refresh is a separate operation. [T06]

**S:** T3's Claude guide documents skill discovery from configuration/project locations and omission of disabled skills from the composer. **U:** that visibility rule does not establish zero imports, zero hooks, zero file/network/process effects, signature enforcement, or manifest-before-import across all providers. [T07]

**I:** The important lifecycle distinction is between the *declared selection*, *discovered menu*, *loaded process generation* and *effective tools/instructions*. A restart only creates an opportunity to align them. It does not certify that disabled artifacts are inert or that an old process has lost authority. Proposed falsifiers are in §7; none were executed.

### 3.3 Continuity, notifications and attention

| Published behavior | What it establishes / does not establish |
|---|---|
| **S:** Locally started Live Activities receive the same ten-minute stale window as relay updates; stale in-flight entries become an out-of-date status, while terminal rows retain their state. [T09] | **I:** explicitly stale presentation is stronger than an indefinitely live-looking indicator. **U:** delivery latency, device correctness and terminal-state freshness were not measured. |
| **S:** Alerts group by environment/thread on APNs and FCM; repeated Android thread alerts get a silent summary. [T10] | **I:** grouping is attention management, not an acknowledgement by the user or completion of a task. |
| **S:** Android foreground suppression is limited to the visible thread, rather than every thread; grouped alerts target the overview. [T11] | **U:** a universal delivered/read/completed receipt model is not established by notification routing. |
| **S:** The optional web/desktop Working section returns threads to the inbox for completion, failure, approval or questions; mobile retains its own list. [T12] | **I:** “needs you” includes several operational states; it is not synonymous with successfully completed. |
| **S:** Claude result correlation was tightened so a background task's result does not prematurely finish the user's compaction turn. [T13] | **I:** native event identity matters more than matching a convenient “result” label. Maintainer replay evidence is not this study's verification. |

**S:** Restart continuation is opt-in and limited to supported threads with saved provider resume state; terminal commands can be interrupted. **U:** recovery without that state is not promised. [T28]

**S:** T3 documents mobile draft/attachment retention and offline message queues, with uploads resuming on reconnect. **I:** that behavior cannot be imported as Recursion's policy: D-036 forbids protected offline redisplay and automatic queued-action replay. A competitor's different choice supplies a comparator, not authorization to reopen the decision. [T08][R01]

**I:** Keep four distinctions in any interpretation: transport delivery, durable acceptance, observed execution outcome, and outstanding operator action. T3 documents several of these separately; the reviewed sources do not establish a single end-to-end receipt covering all four, all providers and all clients. That last cell is **U**, not a claim of absence.

### 3.4 Provider versions, protocols and resilience

**S:** PR #14311 regenerates Codex bindings from `rust-v0.159.0` (`687a119f0fcaace47e1f1abcc77cec6c813fd6da`), replacing 0.156-generated bindings. It distinguishes a recommended version from still-supported versions. The author's live run is reported evidence, not a run performed here. [T15]

**S:** PR #14486 changes Grok CLI versions below 1.0.13 from unknown to broken after documented backend rejection, with manifest updates reaching installed clients separately from app releases. The PR explicitly separates the warning change from another adapter error-handling fix. **I:** compatibility knowledge has its own revision and freshness, not just the application version. [T16]

**S:** PR #10607 describes retiring crashed Grok sessions, failing the active turn, identity-checked cleanup under a per-thread lock, then loading saved ACP state on retry. Its described validation used a mock subprocess, not a live Grok account. **U:** cross-machine recovery and real-provider fault coverage remain unestablished here. [T17]

**S:** The pinned Codex App Server README says persisted `disabledPluginIds` selection does not yet filter plugin capabilities. **I:** a successfully saved control field is not evidence of effective enforcement. This is a narrow versioned statement, not a conclusion about all Codex plugin disabling. [U02]

**I:** A route evidence record needs separate identities for driver, configured instance, harness executable/version, protocol/schema version, compatibility-policy revision, capability set, last successful observation and unknowns. This is a conceptual qualification requirement consistent with D-023; it does not select a schema, binding generator or transport.

### 3.5 Quota and credential handling

**S:** PR #14209 adds a hashed credential fingerprint for OpenCode Go limits because its response lacks account identity. Matching credentials across environments can be merged; different credentials remain separate unless email matching identifies the same account. **I:** deduplication identity is not the same as environment or route identity. The source does not establish a portable billing identity for every provider. [T18]

**S:** T3's usage documentation separates estimated API-equivalent costs from subscription bills, identifies missing history, and notes that API-key/proxy or externally owned server routes may lack subscription limits. It also documents explicit access to a stored Cursor login for account usage. **I:** missing limits must remain unknown; BYOK and subscription balances are different accounting domains. [T19]

**I:** For Recursion, one account visible from two machines must not become two independent allowances. Conversely, two distinct budget owners must not collapse because display labels match. Credential fingerprints can correlate secrets without exposing raw values, but their privacy, rotation, collision and custody implications still require a threat model and counsel. A merged dashboard is neither a reservation ledger nor measured savings.

### 3.6 Mobile stacks and documented tradeoffs

**S:** The SDK migration PR names Expo 58, React Native 0.88 release candidate and native integration changes. It distinguishes tested and untested paths; these are authors' validation reports. **I:** a cross-platform choice still incurs native lifecycle, notification, packaging and dependency work. It proves no comparative performance or porting cost. [T20]

**S:** The Android widget change uses a shared snapshot pipeline with platform-specific layouts; a small native expiry mechanism remains. A documented cold-start tap limitation differs from the running-app path. **I:** sharing UI technology does not make OS semantics identical. [T21]

**S:** A follow-on fix updates Expo to 58.0.2 and Widgets 58.0.11 after an isolated-runtime rendering failure. It requires a native build; OTA cannot replace the extension runtime. The PR explicitly leaves physical-device relay/APNs delivery unverified. [T22]

**S:** The adjacent 2026-10-02 nightly includes that widget repair. The later 0.0.46 preview is described as an unsupported release-pipeline test from a different commit; it is not treated as the v0.0.45 baseline. [T24][T25]

**L/S:** At the same v0.0.45 commit, the root README links store clients while the mobile README still warns of non-distribution. The latter also documents native modules and build variants. This inconsistency prevents treating either sentence as proof of store availability in every region. No store/account check was performed. [T01][T23]

**I:** The native-versus-cross-platform question remains open under D-039. Source-documented native integration work makes both a fully native client and a cross-platform client with native modules credible comparators. Neither is selected. Recursion's online-only privacy and Stop semantics must be evaluated equivalently in either arm.

## 4. Cross-competitor evidence matrix

Each cell is independently evidence-classed. **U** applies to the bounded reviewed evidence, not the product's entire implementation. “Trust” below concerns extension lifecycle; application signing is different.

| Product | Skill/plugin lifecycle | Discovery, trust, disabled-inertness | Continuity / detach / Stop | Mobile stack and OS integration | License / governance |
|---|---|---|---|---|---|
| T3 v0.0.45 | **S:** restart/resume plus discovery refresh. [T06] | **S:** disabled Claude skills omitted from menu. **U:** universal manifest-before-import, signing, zero-effect inertness. [T07] | **S:** opt-in supported restart continuation; no saved resume state means a new message may be needed. [T28] **S:** Stop returns queued messages to draft. [T08] | **S:** Expo/RN with native integration; widgets/Live Activities have platform and build constraints. [T20][T21][T22] | **S:** MIT; maintainer-scoped contribution approval. [T26][T27] |
| Happy CLI baseline | **S:** forwards configured Claude plugin directories. **U:** universal hot reload/restart contract. [H1] | **U:** manifest-first discovery, extension signing, disabled zero-effect guarantee not established. [H3] | **S:** phone handoff restarts into remote mode; keyboard returns local control. [H2] **S:** release documents duplicate-owner prevention and daemon-state recovery. [H1] **U:** ambiguous Stop reconciliation across adapters. | **S:** Expo mobile/web client. [H2] **L:** manifest declares RN and native integration dependencies; dependency presence is not runtime proof. [H4] | **S:** MIT. [H6] **U:** formal governance/maintenance guarantee not established by release evidence. [H1] |
| HAPI | **S:** Pi discovery exists but `/reload` is unavailable over RPC; other harnesses differ. [P2] | **U:** universal extension signatures, manifest-first import and disabled-inertness. **S:** application signing described, a different property. [P4] | **S, live-doc-only:** owner exit ends App Server; attached terminal exit detaches. Stop keeps thread/queue; End archives. Uncertain operations are quarantined without automatic replay. Release binding and runtime correctness **U**. [P5] | **S:** SwiftUI/UIKit iOS, Kotlin Compose Android, APNs/FCM; web/PWA also exists. [P3] **S:** terminal/skill picker and some history operations remain web-only. [P4] | **S:** AGPL-3.0-only; self-hosting and contribution routes documented. [P6][P7] |
| Vibe Kanban | **S:** MCP configuration updates upstream global settings and persists. Hot reload **U**. [V6] | **U:** third-party plugin runtime contract, signing, manifest-first import, disabled inertness. This is not inferred from MCP configuration. [V6] | **S:** switching sessions leaves other agents running; files shared, histories separate; Stop may leave partial edits. Restart recovery and ambiguous Stop acknowledgement **U**. [V5] | **S:** documented phone/browser remote workflow. **U:** native framework and post-sunset remote-service operation. [V7][V8] | **S:** Apache-2.0; shutdown announcement describes community continuation, not a maintenance guarantee. [V2][V8] |
| Conductor | **S:** changed MCP context requires a new session; status refresh alone is insufficient. [C8] **S:** skills supported on selected harnesses. [C9] | **S:** upstream-specific MCP configuration and an enterprise custom-MCP restriction. **U:** import-order, extension signing, inertness enforcement. [C7] | **S:** cloud may continue after app closure; local sessions end. [C5] **S:** API cancel drops queued messages; archive is a different workspace operation. [C4] | **I, vendor-claimed:** iPhone listing. **U:** native/cross-platform framework and Android. [C12] | **S:** public proprietary terms; source/binary license coverage requires counsel. [C14] |
| Upstream baselines | **S:** Claude has surface-limited in-session reload. [U08] **S:** Codex 0.159 disabled-ID persistence is not yet capability filtering. [U02] | **S:** Claude documents integrity checks and plugin code privileges. **U:** these are not a universal inertness proof. [U09] | **S:** native Codex interruption/completion and experimental opt-in documented. [U01] **S:** ACP cancellation permits late updates before final response. [U05] | **U:** protocol support does not establish mobile implementation or OS behavior. [U04] | **S:** Codex/ACP Apache-2.0; Claude repository reserved-rights notice. [U03][U06][U11] |

### Accounting and delegation comparison

| Product | Accounting and upstream-boundary evidence |
|---|---|
| T3 | **S:** credential-based OpenCode Go deduplication and documented missing-limit cases. [T18][T19] **U:** universal reservation/billing reconciliation. |
| Happy | **S:** wrapper/daemon uses upstream authenticated tools; adapter approval differences are documented. [H3] **U:** provider-credential quota aggregation, freshness and authoritative billing. |
| HAPI | **S:** SDK/App Server/ACP routes have differing resume/permission affordances. [P2] **S:** reachable hub does not keep a sleeping executor working. [P6] **U:** credential-scoped quota reconciliation. |
| Vibe Kanban | **S:** context-window gauge documented. [V5] **U:** provider account allowance, key-scoped deduplication and durable cost settlement. |
| Conductor | **S:** billing depends on selected provider account/key and credential precedence. [C10] **S:** Codex account usage/reset display documented. [C11] **U:** multi-credential reconciliation and freshness. |

**S:** Conductor documents a narrow local-script Stop contract: process-group termination followed by escalation, while descendants that leave the group may survive. Its recent changelog also reports a version-specific Stop repair. **I:** this is relevant failure-boundary evidence, not a universal product defect or proof of descendant containment. [C13][C1]

**I:** HAPI's explicit uncertain-write handling is a strong conceptual rival to any Recursion claim of uniqueness in honest recovery. The source is live documentation rather than a verified v0.30.7 contract; test parity and implementation remain unknown. [P5]

**I:** The products expose different abstraction layers: forwarding an upstream plugin directory, managing MCP settings, discovering skills and hosting third-party executable plugins are not equivalent lifecycle guarantees. No row earns a zero or a pass merely from documentation volume.


### 4.1 Upstream lifecycle and protocol controls

**S:** Claude's plugin command reference documents in-session reload; changes affecting MCP tools can be deferred because of prompt-cache invalidation. Non-interactive reload has version/surface limits and does not connect or disconnect plugin MCP servers. **I:** “hot reload” is a capability-by-component-by-surface claim, not a product-wide Boolean. [U08]

**S:** Claude's trust documentation distinguishes tool-call permissions from code executed by hooks/server processes and describes pinned archive/commit integrity checks. **U:** integrity pinning does not establish sandboxing, signer identity, harmless instructions or universally inert disabled artifacts. [U09]

**S:** Its manifest documents versions, dependencies and enablement defaults, including dependency-driven activation. **I:** an explicit dependency graph is not the same as proof of an import-free discovery phase; the latter remains unknown from this document. [U10]

**S:** ACP v1 treats loading history and resuming without replay as separately advertised capabilities. Its cancellation contract permits late updates before the cancelled prompt response, and distinguishes cancellation from normal end-of-turn. **I:** a wrapper must tolerate that interval without representing a sent cancellation as established external rollback. [U04][U05]

## 5. Recursion differentiation: what remains a hypothesis

### 5.1 Table stakes versus candidate advance [I]

Routing among harnesses, multiple clients, worktrees, plugin menus, attention inboxes, persisted sessions, restart recovery and usage views all have prior-art baselines in this report. Even transactional orchestration and explicit authorization boundaries cannot be claimed as unique merely by naming them. Matching those features makes Recursion another entrant in the category; it does not establish D-034's scaling thesis.

| D-034 dimension | Honest claim boundary | Evidence required to distinguish it |
|---|---|---|
| Deterministic composition | **I:** reproducible capability-to-behavior composition is a hypothesis, not “plugins exist” | Same authorized inputs, artifact digests and versions yield the same canonical composition; injection attempts cannot acquire authority; adding a capability has an explicit measured integration cost |
| Authority | **I:** a uniform vocabulary is not uniform enforcement | Revocation/fencing holds at the effect boundary across native and delegated routes, reconnects and plugin generations |
| Productive continuity | **I:** transcript persistence is a weaker endpoint | Authorized work preserves unresolved obligations, evidence, composition identity and accounting through harness change, compaction, fork and machine loss |
| Honest accounting | **I:** a usage chart is not budget correctness | No duplicated allowance, false zero, unowned cost or lost reservation after failover; measured uncertainty accompanies unsupported upstream fields |
| Portability | **I:** published code alone is insufficient | A bounded migration reconstructs control semantics and artifacts without silently widening permissions or acquiring credential custody |

**S:** These are obligations grounded in current accepted decisions, not evidence they have been implemented. The gap register retains open conformance, containment, replay, resource and counsel questions; source research closes none of those experimental gates. [R01][R03]

### 5.2 Strong rival and decisive differentiation falsifier [I]

Use a genuine strong baseline: pinned T3 plus its documented recovery/authority behavior, or a peer whose supported workflow matches the task, and the upstream harness directly. Keep owner authorization, task corpus, subscription/account conditions, resource budgets, evaluator and stop semantics matched. Do not compare a comprehensive Recursion prototype against an artificially stripped peer.

**Decisive falsifier:** a strong baseline preserves the same authorized outcomes and evidence with equal or less operator effort at the frozen budget, or adding a Recursion plugin/node/configuration degrades the relevant endpoint. In either case, withdraw the differentiating claim for that workload/profile. This does not prove Recursion has no value elsewhere, that a vendor intends lock-in, or that one stack is universally superior. Such an evaluation is **UNEXECUTED** here and would need separate admission.

## 6. Licensing, governance and counsel questions

**S:** T3's contributor policy requires prior maintainer approval for significant product changes, narrow scope and evidence, and distinguishes triage from correctness or merge approval. **I:** published source/license and open contribution authority are separate dimensions. Neither popularity nor a permissive license promises review capacity or maintenance continuity. [T27]

**S:** Vibe Kanban's dated shutdown announcement describes a transition toward local/community use; it does not prove later service availability. [V8] **S:** HAPI documents self-hosting and limited namespace arrangements; those are not provider-account isolation findings. [P6]

**S:** Conductor's public terms include competitive-analysis/reverse-engineering restrictions (§2.3) and separate open-source-component language (§2.9). This report records the public terms only, with no account/product use or conclusion about applicability or enforceability. Any subsequent hands-on study requires counsel to resolve the relevant permissions first. [C14]


Questions for counsel, not conclusions:

1. For each pinned component and dependency, which license/notice/patent/trademark obligations apply to distribution, packaging, linking, generated bindings, documentation or derivative work, separately from conceptual interoperability?
2. Does a project's repository license cover its clients, relay, hosted service, icons, fonts, native extensions and bundled third-party components equally? Where are exceptions recorded?
3. For credential-reliant routes, what do current account terms permit for third-party driving, native headless fan-out, concurrency, delegation and subscription use? Does a software license say anything about those service permissions?
4. Which product terms govern public documentation research versus account/product use, particularly commercial comparators? What review is required before any later hands-on study?
5. Does a credential-derived identifier introduce a personal-data/linkability or secret-handling obligation? What account/tenant separation and retention are required?
6. What independent creation and provenance records are needed for Recursion's own interface contracts, given the instruction not to copy competitor code, schemas, assets or proprietary design?
7. How should Recursion's already accepted Apache-2.0 posture, unresolved contribution/CLA details and third-party plugin licenses be reconciled before public distribution?

No route, license combination, security model, account eligibility or reuse is cleared by this study. G-32 remains open. [R03]

## 7. Conceptual learning list and unexecuted falsifiers

Every row is an **I recommendation to investigate a contract**, not a technology decision, implementation plan or authorization to run. Each rival is a credible alternative pattern. All vectors are **UNEXECUTED**.

| Learning / evidence basis | Competing option | Decisive falsifier | What it does not prove |
|---|---|---|---|
| Treat composition changes as versioned generation changes (§3.2, §4.1) | Restart/resume boundary versus component hot reload with explicit quiescence | A declared new generation serves old tools/instructions, loses pending approval identity, or permits old-generation effects after revocation | That restart is safer/faster; either pattern can fail |
| Specify disabled-inertness beyond menu visibility (§3.2, §3.4) | Manifest-only discovery with no import versus loaded-but-denied capability registry | A disabled synthetic artifact starts a process, performs I/O, registers a hook, changes prompt bytes or receives execution authority | That signing, a digest or a setting alone enforces isolation |
| Separate control acknowledgement from execution and attention state (§3.1, §3.3) | Durable intent + later outcome versus a single provider terminal status with explicit unknowns | Lost/duplicated/out-of-order events yield false success, duplicate effects or lost unresolved approvals | Exactly-once external side effects or user receipt |
| Make staleness explicit in remote status (§3.3, §3.6) | Timed stale display versus live-only status with no retained activity | An expired/disconnected view is presented as current or reveals protected content after authority cannot be checked | Remote erasure, reliable OS push or offline revocation knowledge |
| Distinguish route, credential/account and budget identities (§3.5) | Credential-scoped deduplication versus provider-issued opaque account/budget IDs | One allowance is counted twice or distinct budgets merge; key rotation erases outstanding reservations | Actual billed cost, available quota or entitlement from inferred identities |
| Maintain versioned route evidence (§3.4) | Version ranges plus runtime capability negotiation versus only exact-version qualification | A supposedly compatible endpoint lacks a required method/permission semantic or a downgrade replays incompatible stored state | That public protocols are stable forever, or any route is qualified |
| Preserve terminal outcome correlation (§3.3, §4.1) | Native request/turn identities versus wrapper-owned correlation with explicit ambiguity | A background notification completes the wrong turn, or sent Stop becomes “rolled back” without effect evidence | That every upstream exposes the identifiers needed |
| Evaluate shared and native mobile arms under identical contracts (§3.6) | Cross-platform shell plus native modules versus fully native clients | Either arm cannot meet online-only private hydration, revocation, accessibility, cancellation or cold-start status requirements | Selection of Swift/Kotlin/React Native/Expo; measured porting or maintenance savings |
| Keep executor ownership independent of client lifetime (§2.3, §4) | Per-machine owner versus hub-mediated ownership with fencing | Two live owners admit effects or reconnect attaches to a stale generation as current | Automatic takeover, zero downtime or network partition tolerance |
| Record licensing/governance as separate route metadata (§6) | Community extension ecosystem versus tightly maintained first-party adapters | Required updates cannot be reviewed/licensed/distributed within the authorized support contract | Legal clearance or future maintainer behavior |

## 8. Method, threats to validity and change triggers

**L:** Research read the current Recursion brief/control/anchor/decision records and independently retrieved T3 primary sources before consulting the initial competitive note. The note's popularity counts and production-credibility inference were not used as proof. T3's pinned README includes Antigravity, which the initial note's shorter harness list omitted. [R00][R02][R04][T01]

- **Documentation versus execution:** source prose and merged PR descriptions can be stale or wrong. Runtime properties stay unverified until a separately admitted independent observation. A published adverse trace with an exact version could change a contract assessment; it would not establish universal failure.
- **Version churn:** immutable T3 and repository pins prevent accidentally combining generations. Mutable vendor docs are date snapshots and may describe newer releases; their stated minimum versions are retained where relevant. They are not silently attributed to T3's bundled versions.
- **Marketing and popularity:** distribution links, user counts, testimonials and performance slogans do not verify suitability. Marketing-only claims remain vendor-claimed/inferred. No competitor cost/performance figure is transferred to Recursion.
- **Survivorship and selection:** mobile-heavy active projects alone would hide discontinued work and maintenance costs. The sample therefore includes a sunset predecessor and a commercial comparator; it remains non-exhaustive.
- **Unequal visibility:** closed products and sparsely documented features generate more unknowns. Unknown cells are not scored as failures; no weighted winner table is produced.
- **Protocol versus adapter:** a protocol's capability does not establish that an adapter implements it correctly. Both positive and adverse traces, including unsupported capabilities, would be required.
- **Terminology:** skill, plugin, session, completion, Stop, account and resume do not denote identical things across products. The report compares the observable boundary, not matching labels.
- **Conflicting sources:** the T3 mobile README/root README discrepancy and current Recursion D-038 supersession are exposed explicitly. A newer source only replaces a pinned historical assertion after its version/scope is reconciled.

**What would change the main conclusion [I]:** public conformance evidence showing that an existing peer already provides the combined authority/composition/continuity/accounting contract at matched endpoints would strengthen the baseline and narrow Recursion's claim. Evidence that a required upstream cannot express the contract would narrow that route, not justify a fabricated guarantee. Maintenance, mobile and cost rankings require measured whole-workflow evidence, not authoring-speed anecdotes.

## 9. Completion and non-proofs

This is a **research candidate**, not implementation acceptance. The coverage map below addresses all 15 questions and 10 completion gates. Public-source verification is complete for the explicitly documented T3 changes; unknown enforcement/runtime cells remain unknown. Packaging checks inspect the report, references and scope only.

Not established: measured competitor or Recursion performance; subscription savings; security or credential non-access; account eligibility; legal/license compatibility; cheap porting; kernel freeze; qualified adapters; Windows/Linux runtime behavior; store availability; successful push delivery; or source-independent acceptance. No protected repository path was edited. The separate handoff is bounded to later read-only intake unless fresh authority expands it.

### Coverage map

| Brief questions | Report sections |
|---|---|
| Q01 landscape; Q02 architecture patterns | §2.1–2.3 |
| Q03 T3 architecture; Q04 lifecycle; Q05 attention/continuity | §3.1; §3.2; §3.3 |
| Q06 versions/resilience; Q07 accounting; Q08 mobile | §3.4; §3.5 and §4; §3.6 |
| Q09 peer lifecycle; Q10 continuity/Stop; Q11 mobile comparison | §4 and §7; §4; §3.6 and §4 |
| Q12 licensing/governance; Q13 differentiation | §2.2, §4 and §6; §5 |
| Q14 learning/falsifiers; Q15 validity | §7; §8 |

| Completion gates | Where addressed / limit |
|---|---|
| G01 inventory and definition; G02 dated T3 specifics | §2 and §10; §3 |
| G03 peer matrix; G04 differentiation; G05 rivals/falsifiers/non-proofs | §4; §5; §7 |
| G06 counsel questions; G07 evidence classification | §6; evidence vocabulary and cited cells throughout |
| G08 no selections/execution; G09 non-proofs | §1, §7 and §9 |
| G10 protected paths, privacy and secret check | Own deliverables passed a bounded pattern scan; three protected source files matched their pinned Git blob hashes. This is an artifact check, not a security certification or product acceptance |

## 10. Source register

All retrieval dates: **2026-10-02 UTC**. Repository links use full commit hashes. PR descriptions are mutable public records associated with the recorded merge commit; that commit does not freeze the wording of the PR body. Live documentation has no immutable revision supplied by the publisher and is labeled accordingly. Source summaries are deliberately short; no long quotations are included.

- **C1** — [Conductor: Version evidence](https://www.conductor.build/changelog/0.89.1-gpt-6-1-sol). Pin: `0.89.1; patch notes through 0.89.3`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **C10** — [Conductor: Credential-dependent billing](https://www.conductor.build/docs/guides/providers). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **C11** — [Conductor: Account-level usage display](https://www.conductor.build/changelog/0.75.0-polish). Pin: `0.75.0`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **C12** — [Conductor: Mobile distribution](https://apps.apple.com/us/app/conductor-build/id6791228564). Pin: `iOS 1.0.4`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **C13** — [Conductor: Stop does not imply every descendant died](https://www.conductor.build/docs/reference/scripts). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **C14** — [Conductor: Governance and licensing evidence](https://www.conductor.build/terms). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **C2** — [Conductor: Controlled harnesses](https://www.conductor.build/docs/reference/harnesses). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **C3** — [Conductor: Published surfaces](https://www.conductor.build/). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **C4** — [Conductor: Lifecycle API](https://www.conductor.build/docs/api). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **C5** — [Conductor: Closing and reattachment boundary](https://www.conductor.build/docs/cloud/faq). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **C7** — [Conductor: Extension trust boundary](https://www.conductor.build/docs/reference/mcp). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **C8** — [Conductor: Reload semantics](https://www.conductor.build/docs/guides/configure-mcp-servers). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **C9** — [Conductor: Skills and Stop scope](https://www.conductor.build/docs/concepts/agent-modes). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **H1** — [Happy: Release and continuity](https://github.com/slopus/happy/releases/tag/cli-1.2.5). Pin: `4beb3543cfdeb507be838016996f7a0f65cf7b49`. Accessed 2026-10-02. Release/commit metadata URL.
- **H2** — [Happy: Surfaces and handoff](https://github.com/slopus/happy/blob/cli-1.2.5/README.md). Pin: `4beb3543cfdeb507be838016996f7a0f65cf7b49`. Accessed 2026-10-02. Fetched release-tag URL; release mapped to recorded SHA.
- **H3** — [Happy: Controlled harnesses and permissions](https://github.com/slopus/happy/blob/cli-1.2.5/packages/happy-cli/README.md). Pin: `4beb3543cfdeb507be838016996f7a0f65cf7b49`. Accessed 2026-10-02. Fetched release-tag URL; release mapped to recorded SHA.
- **H4** — [Happy: Mobile implementation observation](https://github.com/slopus/happy/blob/4beb3543cfdeb507be838016996f7a0f65cf7b49/packages/happy-app/package.json). Pin: `4beb3543cfdeb507be838016996f7a0f65cf7b49`. Accessed 2026-10-02. Exact SHA URL fetched in full through public GitHub connector.
- **H6** — [Happy: Full pinned license](https://github.com/slopus/happy/blob/4beb3543cfdeb507be838016996f7a0f65cf7b49/LICENSE). Pin: `4beb3543cfdeb507be838016996f7a0f65cf7b49`. Accessed 2026-10-02. Exact SHA URL fetched in full through public GitHub connector.
- **P1** — [HAPI: Release and license](https://github.com/tiann/hapi/commit/0239edf38e2da653d662f31039e24ccea04c7837). Pin: `0239edf38e2da653d662f31039e24ccea04c7837`. Accessed 2026-10-02. SHA in fetched URL.
- **P2** — [HAPI: Controlled harnesses and extension boundary](https://github.com/tiann/hapi/blob/0239edf38e2da653d662f31039e24ccea04c7837/docs/guide/agents.md). Pin: `0239edf38e2da653d662f31039e24ccea04c7837`. Accessed 2026-10-02. SHA in fetched URL.
- **P3** — [HAPI: Control surfaces and transport](https://github.com/tiann/hapi/blob/v0.30.7/docs/guide/how-it-works.md). Pin: `0239edf38e2da653d662f31039e24ccea04c7837`. Accessed 2026-10-02. Fetched release-tag URL; release mapped to recorded SHA.
- **P4** — [HAPI: Mobile integrations and accounting visibility](https://github.com/tiann/hapi/blob/v0.30.7/docs/guide/native-apps.md). Pin: `0239edf38e2da653d662f31039e24ccea04c7837`. Accessed 2026-10-02. Fetched release-tag URL; release mapped to recorded SHA.
- **P5** — [HAPI: Detach, Stop and uncertain outcomes](https://hapi.run/docs/guide/codex-shared-sessions). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **P6** — [HAPI: Execution and governance boundary](https://github.com/tiann/hapi/blob/v0.30.7/docs/guide/faq.md). Pin: `0239edf38e2da653d662f31039e24ccea04c7837`. Accessed 2026-10-02. Fetched release-tag URL; release mapped to recorded SHA.
- **P7** — [HAPI: Full pinned license](https://github.com/tiann/hapi/blob/0239edf38e2da653d662f31039e24ccea04c7837/LICENSE). Pin: `0239edf38e2da653d662f31039e24ccea04c7837`. Accessed 2026-10-02. Exact SHA URL fetched in full through public GitHub connector.
- **R00** — [Exact R-CS research brief](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/prompts/dot-research-20261002e/R-CS-control-surface-and-competitor-study.md). Pin: `699a29d622d1a1d3cecdaf3504cc5d649fb9d20e`. Accessed 2026-10-02.
- **R01** — [Current accepted owner decisions](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/state/DECISIONS.md). Pin: `699a29d622d1a1d3cecdaf3504cc5d649fb9d20e`. Accessed 2026-10-02.
- **R02** — [Operative control plane](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/CONTROL_PLANE.md). Pin: `699a29d622d1a1d3cecdaf3504cc5d649fb9d20e`. Accessed 2026-10-02.
- **R03** — [Research gaps](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/research-lane/GAPS.md). Pin: `699a29d622d1a1d3cecdaf3504cc5d649fb9d20e`. Accessed 2026-10-02.
- **R04** — [Initial T3 competitive note (checked only after independent retrieval)](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/research-lane/competitive-notes/20261002-t3code-v0.0.45.md). Pin: `699a29d622d1a1d3cecdaf3504cc5d649fb9d20e`. Accessed 2026-10-02.
- **T01** — [README: category, harnesses and clients](https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/README.md). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02. Locator: L1-L23.
- **T02** — [T3 release v0.0.45](https://github.com/pingdotgg/t3code/releases/tag/v0.0.45). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02.
- **T03** — [Architecture: ownership and durable intent](https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/internals/overview.md). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02. Locator: L1-L82.
- **T04** — [Environment authentication](https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/internals/environment-auth.md). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02. Locator: L1-L38 and L59-L65.
- **T05** — [Effect services: transport/domain separation](https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/internals/effect-services.md). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02. Locator: L1-L21.
- **T06** — [feat(web): restart the agent session from cmd+k to load new skills and plugins](https://github.com/pingdotgg/t3code/pull/14542). Pin: `921cb3c8bc772309ccab4c422b091d0a91497a49`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T07** — [Claude provider guide: skills](https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/user/providers-claude.md). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02. Locator: Skills section.
- **T08** — [Messages and context](https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/user/composer.md). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02. Locator: L35-L57 and L146-L163.
- **T09** — [feat(mobile): degrade the agent Live Activity once its content goes stale](https://github.com/pingdotgg/t3code/pull/12047). Pin: `09388bf2da51b958fe5c11fb5bade155fe575e58`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T10** — [feat(notifications): stack agent alerts by thread on both platforms](https://github.com/pingdotgg/t3code/pull/12048). Pin: `438af295fefdd04cc38629f902d7faa07cf39712`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T11** — [feat(mobile): suppress only the on-screen thread's alert on Android](https://github.com/pingdotgg/t3code/pull/12052). Pin: `bed69f6ea41dab13ec37afbaaa2c911a19865f7a`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T12** — [feat(web): beta Working section hides busy threads until they need you](https://github.com/pingdotgg/t3code/pull/13926). Pin: `b33eda13998b5b0571daaf266c87f6f31af41f7d`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T13** — [fix(server): Claude /compact no longer ends early and leaves the thread busy](https://github.com/pingdotgg/t3code/pull/14497). Pin: `9da066dbe906310671b86290b09df0b56a88ffa5`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T14** — [Provider constraints](https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/internals/providers.md). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02. Locator: L1-L66 and L88-L115.
- **T15** — [feat(codex): regenerate protocol bindings for Codex 0.159](https://github.com/pingdotgg/t3code/pull/14311). Pin: `422248515a1eebff607b44d5abef91e13f6228fc`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T16** — [fix(server): Grok CLIs older than 1.0.13 are marked broken](https://github.com/pingdotgg/t3code/pull/14486). Pin: `bd89c1302026255c62cc09278207bfaf2664da4a`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T17** — [fix(grok): recover from crashed provider sessions](https://github.com/pingdotgg/t3code/pull/10607). Pin: `050cfad04f673fb092630614b8f7e82000b43ea5`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T18** — [fix(shared): merge OpenCode Go limits by credential](https://github.com/pingdotgg/t3code/pull/14209). Pin: `1a553d0f5dd2aa39a64895ec770dc51ef1448b1a`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T19** — [Usage and limits](https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/user/usage.md). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02. Locator: L7-L42 and L65-L106.
- **T20** — [chore(mobile): upgrade to Expo SDK 58 and React Native 0.88 RC](https://github.com/pingdotgg/t3code/pull/12045). Pin: `8dc07f199c2e3a4d8ec1a3997a2b9c2edf12457e`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T21** — [feat(mobile): render the Android subscription widget with expo-widgets](https://github.com/pingdotgg/t3code/pull/12046). Pin: `e41755cf8a9b07a12fefee5134cadacb514d4ad9`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T22** — [fix(mobile): update Expo 58 to restore widgets and Live Activities](https://github.com/pingdotgg/t3code/pull/14734). Pin: `54084ae1e6c32809db040e4fa571c80fdf2d8ae4`. Accessed 2026-10-02. merge commit pins code association; PR body is mutable.
- **T23** — [Mobile README](https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/apps/mobile/README.md). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02. Locator: Distribution warning; variants; native modules.
- **T24** — [T3 release v0.0.45-nightly.20261002.2584](https://github.com/pingdotgg/t3code/releases/tag/v0.0.45-nightly.20261002.2584). Pin: `54084ae1e6c32809db040e4fa571c80fdf2d8ae4`. Accessed 2026-10-02.
- **T25** — [T3 release v0.0.46-preview.20261002.2598](https://github.com/pingdotgg/t3code/releases/tag/v0.0.46-preview.20261002.2598). Pin: `b37fcea42f43ca1276eea20d5c7ea355e16a169a`. Accessed 2026-10-02.
- **T26** — [MIT license](https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/LICENSE). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02. Locator: Full license.
- **T27** — [Contribution governance](https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/CONTRIBUTING.md). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02. Locator: Approval, verification and triage sections.
- **T28** — [Updates and restart continuation](https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/user/updating.md). Pin: `6c8fed35dded9ff71c5b46807125457acbb76be6`. Accessed 2026-10-02. Locator: Before you update section.
- **U01** — [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Locator: Initialization; API overview; experimental opt-in; interruption.
- **U02** — [Codex App Server README](https://github.com/openai/codex/blob/687a119f0fcaace47e1f1abcc77cec6c813fd6da/codex-rs/app-server/README.md). Pin: `687a119f0fcaace47e1f1abcc77cec6c813fd6da; rust-v0.159.0`. Accessed 2026-10-02. Locator: Thread plugin settings.
- **U03** — [Codex license](https://github.com/openai/codex/blob/687a119f0fcaace47e1f1abcc77cec6c813fd6da/LICENSE). Pin: `687a119f0fcaace47e1f1abcc77cec6c813fd6da; rust-v0.159.0`. Accessed 2026-10-02. Locator: Apache-2.0 full license read.
- **U04** — [ACP v1 session setup](https://github.com/agentclientprotocol/agent-client-protocol/blob/5cee3927b6ac840a6c3e0f3d2569a5551eca20ee/docs/protocol/v1/session-setup.mdx). Pin: `5cee3927b6ac840a6c3e0f3d2569a5551eca20ee`. Accessed 2026-10-02. Locator: Loading and resuming capabilities; website equivalent https://agentclientprotocol.com/protocol/v1/session-setup.
- **U05** — [ACP v1 prompt turn](https://github.com/agentclientprotocol/agent-client-protocol/blob/5cee3927b6ac840a6c3e0f3d2569a5551eca20ee/docs/protocol/v1/prompt-turn.mdx). Pin: `5cee3927b6ac840a6c3e0f3d2569a5551eca20ee`. Accessed 2026-10-02. Locator: Cancellation; website equivalent https://agentclientprotocol.com/protocol/v1/prompt-turn.
- **U06** — [ACP license](https://github.com/agentclientprotocol/agent-client-protocol/blob/5cee3927b6ac840a6c3e0f3d2569a5551eca20ee/LICENSE). Pin: `5cee3927b6ac840a6c3e0f3d2569a5551eca20ee`. Accessed 2026-10-02. Locator: Apache-2.0 full license read.
- **U07** — [Claude Code Remote Control](https://code.claude.com/docs/en/remote-control). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Locator: Connect from another device; session recovery; surface-specific limits.
- **U08** — [Claude plugin commands](https://code.claude.com/docs/en/plugins/cli-reference). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Locator: Reload-plugins; non-interactive behavior requires v2.1.260+.
- **U09** — [Claude plugin security and trust](https://code.claude.com/docs/en/plugins/security). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Locator: Execution permissions; integrity checks.
- **U10** — [Claude plugin manifest reference](https://code.claude.com/docs/en/plugins-reference). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Locator: Version; defaultEnabled; dependencies.
- **U11** — [Claude Code repository license notice](https://github.com/anthropics/claude-code/blob/1c229fcd1e1e4e452e29a8f116b45fe4cfe2c528/LICENSE.md). Pin: `1c229fcd1e1e4e452e29a8f116b45fe4cfe2c528`. Accessed 2026-10-02. Locator: All-rights-reserved notice and Commercial Terms link.
- **V1** — [Vibe Kanban: Pinned version](https://github.com/BloopAI/vibe-kanban/commit/4deb7eca8f381f7cbc1f9d15515a9ab8f8009053). Pin: `4deb7eca8f381f7cbc1f9d15515a9ab8f8009053`. Accessed 2026-10-02. SHA in fetched URL.
- **V2** — [Vibe Kanban: License](https://github.com/BloopAI/vibe-kanban/blob/4deb7eca8f381f7cbc1f9d15515a9ab8f8009053/LICENSE). Pin: `4deb7eca8f381f7cbc1f9d15515a9ab8f8009053`. Accessed 2026-10-02. SHA in fetched URL.
- **V3** — [Vibe Kanban: Orchestration and surfaces](https://github.com/BloopAI/vibe-kanban/blob/4deb7eca8f381f7cbc1f9d15515a9ab8f8009053/README.md). Pin: `4deb7eca8f381f7cbc1f9d15515a9ab8f8009053`. Accessed 2026-10-02. SHA in fetched URL.
- **V5** — [Vibe Kanban: Continuity and usage](https://github.com/BloopAI/vibe-kanban/blob/4deb7eca8f381f7cbc1f9d15515a9ab8f8009053/docs/workspaces/sessions.mdx). Pin: `4deb7eca8f381f7cbc1f9d15515a9ab8f8009053`. Accessed 2026-10-02. SHA in fetched URL.
- **V6** — [Vibe Kanban: Extensions and persistence](https://vibekanban.com/docs/settings/mcp-servers). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **V7** — [Vibe Kanban: Mobile access caveat](https://vibekanban.com/docs/remote-access). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.
- **V8** — [Vibe Kanban: Governance status change](https://www.vibekanban.com/blog/shutdown). Pin: `live documentation; observed 2026-10-02`. Accessed 2026-10-02. Live public source; no verified repository version binding.

[C1]: https://www.conductor.build/changelog/0.89.1-gpt-6-1-sol
[C10]: https://www.conductor.build/docs/guides/providers
[C11]: https://www.conductor.build/changelog/0.75.0-polish
[C12]: https://apps.apple.com/us/app/conductor-build/id6791228564
[C13]: https://www.conductor.build/docs/reference/scripts
[C14]: https://www.conductor.build/terms
[C2]: https://www.conductor.build/docs/reference/harnesses
[C3]: https://www.conductor.build/
[C4]: https://www.conductor.build/docs/api
[C5]: https://www.conductor.build/docs/cloud/faq
[C7]: https://www.conductor.build/docs/reference/mcp
[C8]: https://www.conductor.build/docs/guides/configure-mcp-servers
[C9]: https://www.conductor.build/docs/concepts/agent-modes
[H1]: https://github.com/slopus/happy/releases/tag/cli-1.2.5
[H2]: https://github.com/slopus/happy/blob/cli-1.2.5/README.md
[H3]: https://github.com/slopus/happy/blob/cli-1.2.5/packages/happy-cli/README.md
[H4]: https://github.com/slopus/happy/blob/4beb3543cfdeb507be838016996f7a0f65cf7b49/packages/happy-app/package.json
[H6]: https://github.com/slopus/happy/blob/4beb3543cfdeb507be838016996f7a0f65cf7b49/LICENSE
[P1]: https://github.com/tiann/hapi/commit/0239edf38e2da653d662f31039e24ccea04c7837
[P2]: https://github.com/tiann/hapi/blob/0239edf38e2da653d662f31039e24ccea04c7837/docs/guide/agents.md
[P3]: https://github.com/tiann/hapi/blob/v0.30.7/docs/guide/how-it-works.md
[P4]: https://github.com/tiann/hapi/blob/v0.30.7/docs/guide/native-apps.md
[P5]: https://hapi.run/docs/guide/codex-shared-sessions
[P6]: https://github.com/tiann/hapi/blob/v0.30.7/docs/guide/faq.md
[P7]: https://github.com/tiann/hapi/blob/0239edf38e2da653d662f31039e24ccea04c7837/LICENSE
[R00]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/prompts/dot-research-20261002e/R-CS-control-surface-and-competitor-study.md
[R01]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/state/DECISIONS.md
[R02]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/CONTROL_PLANE.md
[R03]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/research-lane/GAPS.md
[R04]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/research-lane/competitive-notes/20261002-t3code-v0.0.45.md
[T01]: https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/README.md
[T02]: https://github.com/pingdotgg/t3code/releases/tag/v0.0.45
[T03]: https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/internals/overview.md
[T04]: https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/internals/environment-auth.md
[T05]: https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/internals/effect-services.md
[T06]: https://github.com/pingdotgg/t3code/pull/14542
[T07]: https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/user/providers-claude.md
[T08]: https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/user/composer.md
[T09]: https://github.com/pingdotgg/t3code/pull/12047
[T10]: https://github.com/pingdotgg/t3code/pull/12048
[T11]: https://github.com/pingdotgg/t3code/pull/12052
[T12]: https://github.com/pingdotgg/t3code/pull/13926
[T13]: https://github.com/pingdotgg/t3code/pull/14497
[T14]: https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/internals/providers.md
[T15]: https://github.com/pingdotgg/t3code/pull/14311
[T16]: https://github.com/pingdotgg/t3code/pull/14486
[T17]: https://github.com/pingdotgg/t3code/pull/10607
[T18]: https://github.com/pingdotgg/t3code/pull/14209
[T19]: https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/user/usage.md
[T20]: https://github.com/pingdotgg/t3code/pull/12045
[T21]: https://github.com/pingdotgg/t3code/pull/12046
[T22]: https://github.com/pingdotgg/t3code/pull/14734
[T23]: https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/apps/mobile/README.md
[T24]: https://github.com/pingdotgg/t3code/releases/tag/v0.0.45-nightly.20261002.2584
[T25]: https://github.com/pingdotgg/t3code/releases/tag/v0.0.46-preview.20261002.2598
[T26]: https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/LICENSE
[T27]: https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/CONTRIBUTING.md
[T28]: https://github.com/pingdotgg/t3code/blob/6c8fed35dded9ff71c5b46807125457acbb76be6/docs/user/updating.md
[U01]: https://learn.chatgpt.com/docs/app-server
[U02]: https://github.com/openai/codex/blob/687a119f0fcaace47e1f1abcc77cec6c813fd6da/codex-rs/app-server/README.md
[U03]: https://github.com/openai/codex/blob/687a119f0fcaace47e1f1abcc77cec6c813fd6da/LICENSE
[U04]: https://github.com/agentclientprotocol/agent-client-protocol/blob/5cee3927b6ac840a6c3e0f3d2569a5551eca20ee/docs/protocol/v1/session-setup.mdx
[U05]: https://github.com/agentclientprotocol/agent-client-protocol/blob/5cee3927b6ac840a6c3e0f3d2569a5551eca20ee/docs/protocol/v1/prompt-turn.mdx
[U06]: https://github.com/agentclientprotocol/agent-client-protocol/blob/5cee3927b6ac840a6c3e0f3d2569a5551eca20ee/LICENSE
[U07]: https://code.claude.com/docs/en/remote-control
[U08]: https://code.claude.com/docs/en/plugins/cli-reference
[U09]: https://code.claude.com/docs/en/plugins/security
[U10]: https://code.claude.com/docs/en/plugins-reference
[U11]: https://github.com/anthropics/claude-code/blob/1c229fcd1e1e4e452e29a8f116b45fe4cfe2c528/LICENSE.md
[V1]: https://github.com/BloopAI/vibe-kanban/commit/4deb7eca8f381f7cbc1f9d15515a9ab8f8009053
[V2]: https://github.com/BloopAI/vibe-kanban/blob/4deb7eca8f381f7cbc1f9d15515a9ab8f8009053/LICENSE
[V3]: https://github.com/BloopAI/vibe-kanban/blob/4deb7eca8f381f7cbc1f9d15515a9ab8f8009053/README.md
[V5]: https://github.com/BloopAI/vibe-kanban/blob/4deb7eca8f381f7cbc1f9d15515a9ab8f8009053/docs/workspaces/sessions.mdx
[V6]: https://vibekanban.com/docs/settings/mcp-servers
[V7]: https://vibekanban.com/docs/remote-access
[V8]: https://www.vibekanban.com/blog/shutdown
