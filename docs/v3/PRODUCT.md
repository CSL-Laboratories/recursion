---
title: Recursion v3 product specification direction
status: amendment proposed (2026-10-24); acceptance is a human decision by Lucas
last-updated: 2026-10-25
prior-status: accepted-direction-with-open-research
---

# Recursion v3 product hypothesis

## Accepted working foundation and required collaboration â€” 2026-09-07

Recursion should stay closely aligned with the userâ€™s goals and adapt its working style to the task. It should support extensive autonomous research and planning when requested, and close collaboration when the user wants to develop something together. Within agreed boundaries, it should use initiative and carry work through. Throughout that work, it should preserve intent, incorporate feedback, surface consequential changes, and remain easy to inspect, redirect and stop.

The paragraph above is exact Owner-approved wording (OR-038). Requested direction also requires
innovative agent communication, terminal multiplexing and coordinated work across compute
instances and nodes. Communication, delegation and interaction are required capabilities.
Research compares mechanisms, activation, defaults and placement in kernel, service or plugin;
a narrow quota result cannot veto their existence. Rich and lean configurations preserve access
to accepted capabilities without requiring every optional component to remain resident.

The representative workflow is an Owner working with a questionnaire assistant that consults
Head directly, returns clarifications and lets the Owner observe attributable exchange,
participate, redirect and stop it. Head integrates exact accepted answers and repeats research
toward concrete architecture and bounded delegation. Model, session and node may differ while
goal, authority, decision provenance and aggregate accounting stay coherent.

OR-039 makes configuration, compatibility and whole-system optimization central. Evaluate
cognitive/coordination burden, intent continuity, clarity, Owner control, quality and specialized
assistance alongside OR-024's existing ranking and floors. Additional consumption can be a
chosen tradeoff; no universal feature-cost or plugin-efficiency claim follows.

## Amendment proposed 2026-10-24 — frontier-defining harness

**Status: amendment proposed (2026-10-24); acceptance is a human decision by Lucas.**

This section **adds** Lucas's 2026-10-24 direction. It does not rewrite the OR-038 paragraph
above. Full pillar designs, competitor citations and the file-by-file inventory live in
[AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md). Publishing this text does not admit
implementation work, clear a hold, select a language, or authorize spend.

### North star

Recursion is **the frontier-defining harness** — "the goat": a first-class native harness
and a first-class unifying wrapper meant to beat any harness in the way, including Octopus
(which Octopus is research question F-1), the DeepSeek harness (`deepseek-ai/deepseek-harness`),
Hermes Agent, T3 Code, OpenCode, Claude Code, Codex, and more.

The accepted public thesis remains **"the harness that scales with your workflow"** (D-034).
This amendment states the competitive bar that thesis must now earn. Nov 11, 2026 is
everything that can fit, with no shrinking of ambition. A public demo is required. A private
held-out eval is allowed. No SWE-bench chasing. Linux-first sprint for sandboxes and nodes;
product surfaces target Linux, Windows and macOS.

### Seven-point bar

Every parent and demo is scored against Lucas's seven-point bar (frontier map, 2026-10-25).
A plan that defers the GUI fails bar 4.

1. Researched and developed, with evidence (unknown ≠ pass).
2. Practical for enterprise and developers (no credential custody, truthful Stop, audit receipts).
3. Hyper-optimized (OR-024 visible; GUI attach launches zero inference).
4. A real graphical interface users interact with (same daemon as the CLI; window-close ≠ kill).
5. Directed and installed by agents (Control MCP / control API).
6. Config syncs with providers and preferences already in use (never copy tokens).
7. Loved features done correctly (plan, diffs, worktrees, Stop, fork, approvals, PR-as-publish,
   honest usage — generated ≠ accepted, unknown ≠ 0, MCP ≠ grant).

### Pillars

Core: calling and piloting other subscriptions (Claude Max, Codex, Cursor, Devin, Grok Bot,
OpenCode Go/Zen); cross-chat; remote control; enterprise capabilities; node orchestration
across machines. Also: verification, quota/cost ledger, memory and learning, plugin
portability, automation, sandboxing, observability, research mode, evaluation.

Method: **design as you research, develop only once grounded, then test** — three phases with
gates. Live Codex and Claude Code runs go on **subscriptions**, with **no dollar spend limit**,
usage tracked against quotas. **No pay-per-token for CSL.**

BYOK keys go in the **OS keyring**. First providers: **OpenRouter, OpenCode Go, OpenCode Zen**.

### Surfaces (what this amendment adds beyond D-038)

D-038 (accepted 2026-10-02) already made GUI and mobile **built but opt-in** in the first
release; no capability is deferred by the old "GUI later" exclusion wording. This amendment
does not reopen D-038.

**Beyond D-038** it specifies: one headless daemon; a daemon-served web GUI in a thin
**Tauri** shell for Linux, Windows and macOS; a standalone app; a CLI/TUI; plus phone/web
remote; and the "goat" bar (seven-point bar). Tauri is the GUI-shell direction (Lucas
R-Q4); it is not a product-language selection (OR-017 / D-039). Requiring the GUI, to the
exclusion of CLI-only use, remains a non-goal. Historical "GUI later" sentences are kept;
dated pointers send readers here. `ANCHOR.md` is not edited.

### Agent operators

Recursion must be operable by agent software via **MCP or a control API**, explicitly
including Grok Bot, and Anthropic's and OpenAI's own agent/bot products (Lucas's working
labels: Claude Bard, Codex Dots); exact products and their control surfaces are a RESEARCH
question, decide from findings (F-15). This amendment does not assert product names for
those labels. Lucas (2026-10-24 11:53pm PT): "Claude has their own bot, not sure what
it's called."

### Research questions, not decisions

F-1 which Octopus; F-3 product language; F-4 what enterprise means by Nov 11; F-9 default
remote relay; F-10 durable execution vs the replay kernel (PR #292); F-15 Anthropic's and
OpenAI's own agent/bot products (working labels: Claude Bard, Codex Dots); and F-5, F-6,
F-7, F-8, F-11, F-12, F-13, F-14. F-2 (GUI contradiction) is answered by D-038; this
amendment adds the Surfaces paragraph above. Full questions and sources:
`CSL-Laboratories/csl-research` commit `250fdbbe2964e4889096a487e2dfb6d7794046a6`,
path `submissions/20261025-recursion-csl494-rp0-amendment-research-01/` (candidate
evidence; vault PR merged).

### Research and open questions

Lucas (2026-10-24 11:57pm PT): the Recursion spec must be dynamic, changing with the
issues and the research that is published. Research lives in
`CSL-Laboratories/csl-research`, not in this repository. This amendment's record:
commit `250fdbbe2964e4889096a487e2dfb6d7794046a6`, path
`submissions/20261025-recursion-csl494-rp0-amendment-research-01/`. Claim: RP0
amendment research grounding, F-1…F-15, and the parent-keyed register for CSL-494.
Limitation: candidate evidence; vault PR
https://github.com/CSL-Laboratories/csl-research/pull/41 merged. Not human
product acceptance.

**Accepted text in this file changes only by a dated amendment that is accepted.**
Lucas-authored dated amendments (including this RP0 text) are accepted when Lucas
accepts. Spec changes authored by Samir or Dan (or their agents, attributed) are
accepted when a founder other than the author approves the PR; that approval counts
as acceptance (Lucas 2026-10-24 11:58pm PT). A research-record update is not
acceptance.

## Mark 2 capability map â€” research synthesis 2026-09-07

The first usable release includes native, independently useful upstream-only and combined
workflows. The accepted ranking is responsiveness, subscription efficiency, then completion,
subject to correctness, authorization, resource limits and recoverability. This research does
not narrow the seven critical names or reopen the ten completed answers. Mechanisms below are
proposals; source support and unrun tests are bound in [evidence](foundation-research/mark2-research/evidence.json).
MKR-001/MKR-008/MKR-022.

| Direction | Required capability and responsible component | Configuration and evidence | Decisive next test / milestone |
|---|---|---|---|
| Q1 / OR-026 | Shared route registry and deterministic setup detect existing installs; adapter supplies version, auth, billing and control capabilities | Installation owner, pin, route descriptor, managed/manual updates; 13 entries and 23 paths in [matrix](foundation-research/mark2-research/compatibility.json) | Interface/eligibility probe before model freeze; seven names required at first release |
| Q2 / OR-027 | Persistent assistant identity projects profiles, sessions and Head/ARM/worker leases; runtime owns consultation and idle lifecycle | Memory audience/freshness, decision classes, route/role/pool; MKR-009/MKR-010 | Assistant lifecycle/consultation family; prove bounded routine delegation and rejection of unauthorized amendments |
| Q3 / OR-028 | Shared terminal/interaction store and process broker implement all ten controls, three Stops and preserved forks | Turn targeting, Stop scope, disclosure timing, lineage and reservation split; MKR-011/MKR-012 | Negative control/fork traces on Windows/Linux and required upstream routes |
| Q4 / OR-029 | Capability compiler and native/upstream plugin adapters preserve behavior, report losses and support local execution | Four incompatibility responses; two revision propagation modes; no hidden fallback; MKR-013/MKR-014 | Configuration/plugin family including browser/MCP research workflow |
| Q5 / OR-030 | Context governor and accounting ledger retain handoff quality and all-role consumption | Capsule validity, compactor ownership, per-role pool and unavailable counters; MKR-002/MKR-019 | Upstream-only full workflow with API credentials absent; continuation fault probes |
| Q6 / OR-031 | Verifier service produces tests run, supported fix confidence, regressions and demonstrated benefit | Risk/independence class and lean reporting preset; MKR-020 | Blind artifact assessment plus full verification/rework cost |
| Q7 / OR-032 | Recovery coordinator preserves uncertainty and enforces relative additional allowance | Destination, automation, exact fraction/unit, cumulative reservations; MKR-016 | Repeated-failure, unknown-counter and shared-pool accounting family |
| Q8 / OR-033 | Release manager and broker distinguish six update modes and four restoration outcomes | Installation ownership, release/state compatibility, per-host landing and resume/hold; MKR-015/MKR-017 | Update/Stop ordering, crash and partition trials |
| Q9 / OR-034 | Context and workflow services reduce repeated instructions and escaped defects where measured | Cold/warm/no-future-work; changed instructions and complete cost; MKR-019/MKR-020 | Paired direct-engine versus platform continuation/performance trials |
| Q10 / OR-035 | Deterministic resource governor admits representative profiles and operator hard ceilings | CPU/RAM/disk/network/queue/review/pool bounds; personal inventory intentionally unspecified | Calibrate minimum/useful profiles after baseline; never infer requirements from this workstation |

Required compatibility names map to researched entries: Grok â†’ Grok Build; Cursor â†’ first-party
SDK/ACP and independent ACP paths; Gemini â†’ Gemini CLI; Claude â†’ Claude Code/SDK; Hermes â†’
Nous Research Hermes Agent; OpenCode â†’ Anomaly OpenCode; ChatGPT â†’ eligible Codex sign-in path.
Codex remains its own requested entry. BYOK is a category; Muse Code is Meta's terminal beta;
DeepSeek Harness and Antigravity local/managed paths are separately identified. Grok Bot and
Pi are assistant references; Perplexity is a prospective research-quality comparator.

Every requirement has three implementation locations: the common deterministic service owns
identity, admission, state and evidence; a native loop implements reasoning/tool behavior; an
upstream adapter composes a supported harness interface and extensions. A source-documented
route with missing behavior is not a complete release configuration. Inspect richer interfaces,
then expose the gap and next test; a still-missing critical name requires an Owner release
decision. No such omission is approved here.

## Mark 2 Owner direction â€” 2026-09-06

OR-026 through OR-035 extend the release direction: seven launch-critical compatibility names (Grok, Cursor, Gemini, Claude, Hermes, OpenCode, ChatGPT), persistent assistants across sessions and goals, local plugin execution, configurable update/recovery behavior and quality-preserving efficiency. Missing critical compatibility requires an Owner release decision. Exact routes and guarantees remain research work. Use the [Mark 2 handoff](foundation-research/mark2/handoff.md); product requirements must not be narrowed to one operator's current hardware or accounts.

## Accepted first usable platform release â€” 2026-09-06

OR-021â€“OR-025 clarify the release under `docs/v3`; earlier brainstorming "v2" means this
milestone. Recursion is the common configurable agent platform across models, harnesses,
subscriptions, tools, workflows and execution machines. The terminal exposes the initial
platform. D-038 in `state/DECISIONS.md` supersedes the older GUI/mobile exclusions:
GUI and mobile clients are to be built as opt-in clients for this release.

Both a small native model-and-tool loop and supported upstream harness execution are release
requirements. Either works independently when its configured route supports the workflow;
combined workflows retain coherent task state and configuration. Upstream-only installations
must provide explicit execution/accounting for every reasoning role without a hidden API key
or separate model connection. Ordinary persistent runtime work requires no continuous inference.

All ten terminal controls, useful engine-specific extensions, configuration/presets/provenance,
plugins with native/upstream implementations, guided setup/import/repair, compaction and
Windows/Linux remote control/execution/recovery require a working minimum. Developers lead
initial audience validation, with Lucas as the first validation user; novice usability and
eventual organization needs remain architectural inputs. macOS is an additional test opportunity.

The [repository map](foundation-research/platform-round/repository-map.md) distinguishes
implemented substrate, inert fixtures, specified requirements and missing proof. The broad
platform ambition is carried through [six research deliverables](foundation-research/platform-round/research-brief.md);
no framework, language, store or complete integration is selected by this ruling.

## Audience and execution-mode findings â€” 2026-09-05

Historical September 5 finding, refined by OR-021 above: Lucas-first validation remains the
starting point. Developers are now the selected initial audience, performing bounded repository
tasks with inspectable diffs and recoverable interruptions. Casual
users need unassisted setup, understandable permissions/cost and recoverable failures; organization
operators additionally need identity, audit, secret and support controls. Hosted tenancy is a
separate deferred authority/isolation design. Enterprise spending and viral adoption remain
hypotheses; no commercial tier policy or compliance claim follows. FND-008/FND-017.

One Recursion CLI can serve native and upstream-engine modes, with visible capability limits.
The product's stable promise is Recursion-owned goals, authority, evidence and recovery; universal
upstream feature parity and subscription portability are unproved. Proposed route-specific
authentication wording still needs route-specific evidence. The historical cohort question in
[foundation decisions](foundation-research/decisions.json) is resolved by the clarification above. See the
[audience job analysis](foundation-research/evidence.json), `analysis.foundation.audiences`.

## Thesis

Recursion is a community, open-source, self-hosted agent harness for conversation, coding, and sustained high-quality autonomous work across models, subscriptions, APIs, CLIs, repositories, and machines. It turns an owner's intent into reviewable work while preserving authority, privacy, quota, machine responsiveness, and evidence.

Its public thesis is **"the harness that scales with your workflow"**: a capable baseline on its
own, that grows deterministically and without lock-in as the user adds subscriptions, BYOKs,
plugins, configurations, nodes and modding, composing the effective system prompt and capabilities
to fit the workflow. All open source. That thesis is a claim to earn, not an assumption: the
composition must be reproducible, versioned, cache-aware and injection-safe, and "scales" is
measured, not asserted (`state/DECISIONS.md` D-034).

Recursion is two things at once: a **first-class native harness** with its own model-and-tool
loop, and a **first-class unifying wrapper** over the wider harness ecosystem (for example Claude
Code, Codex, Cursor, Gemini/Google CLI, Grok Build, Hermes, OpenCode, Devin, Cline, Antigravity,
Pi, Command Code and future adapters). It is not a naive router: it owns the durable
orchestration harness — actor identity, goals, delegation, routing, state, context, tools,
evidence, policy and observation — and it may delegate an individual turn to an optimized
external agent loop without becoming a thin wrapper around it. Its purpose is to **remove
ecosystem lock-in** and give users optimized performance on the models and subscriptions they
already prefer, including by reducing unnecessary quota/usage burn:

- the owner retains sovereignty and final authority;
- one main assistant maintains strategic continuity;
- goal-scoped ARM executives plan and delegate implementation;
- native and external agent loops remain replaceable execution engines behind Recursion's contracts;
- Recursion does not hold or relay provider credentials it was not explicitly granted; where
  possible it delegates to a user-authenticated tool (`state/DECISIONS.md` D-021);
- deterministic infrastructure enforces authority, budget, state, and publication;
- context and evidence survive model, provider, session, and machine changes;
- results are measured by verified code quality, not output volume.

## Initial user and deployment

The first user is Lucas. The first deployment is a lightweight coordinator on the existing coordinator host, with heavier work placed on subscriptions, cloud execution, or authenticated registered nodes. The interactive CLI is the canonical local configuration and operation surface; an agent is never required to edit settings. Multiple detachable terminal windows can observe and converse with Head, ARMs and executors without owning their processes. Telegram is an optional interactive surface for Head and ARM conversations. D-038 in `state/DECISIONS.md` requires GUI and mobile clients to be built as opt-in clients and confirms that D-034 and D-019 supersede older first-release exclusions.

Public usability remains an explicit design requirement. Users should be able to install a useful minimal edition, bring their own official subscriptions or API credentials, select more capable bundles, and retain complete access to human-readable configuration.

Amendment proposed 2026-10-24 (acceptance is a human decision by Lucas; see
[AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md)): the CLI remains a complete operator
surface, so an agent is still never *required* to edit settings, and Recursion is also
**agent-operable** via MCP or a control API (Grok Bot, and Anthropic's and OpenAI's own
agent/bot products — Lucas's working labels: Claude Bard, Codex Dots; exact products and
their control surfaces are F-15). D-038 already put GUI and mobile in the first release as
built-but-opt-in clients; this amendment adds the daemon-served Tauri shell, standalone
app and "goat" bar.

## Product promises

1. **Owner sovereignty.** Local operation, self-hosting, provider choice, portable configuration,
   custom builds and full-access modes remain available. Explicit maximum `sovereign` authority
   may include receipt-backed administration of its own grants.
2. **Provider neutrality.** A model, delivery route, billing pool, harness, and host are separate objects. Users can combine Codex, Cursor, Claude Code, Gemini CLI, OpenCode, Hermes, local models, APIs, and future adapters without rewriting goals.
   The versioned Recursion release is the sole canonical authority and recovery dependency;
   engines and providers are optional typed adapters, while ordinary pinned libraries remain
   permitted implementation dependencies.
3. **Durable autonomy.** Parent goals, ARM state, exact directives, checkpoints, evidence, and budgets survive process death and provider changes.
4. **Quality before activity.** Machine gates and independent verification determine progress. Branch count, agent count, and token consumption do not.
5. **Configurable optimization.** Users may optimize toward cost, quota conservation, quality, latency, privacy, local performance, or custom trade-offs.
6. **Explainability.** Routing, admission, compaction, escalation, and publication decisions are inspectable without spending model tokens.
7. **Privacy by construction.** Project data classification, egress policy, credential isolation, and local-only telemetry are explicit.
8. **Graceful scarcity.** Subscription exhaustion, provider refusal, node loss, and budget depletion checkpoint work instead of encouraging uncontrolled fallback.
9. **Evidence-grounded research.** Research ARMs can find papers, standards, datasets,
   documentation and source; reconcile contradiction; propose experiments; and preserve
   citations without treating model consensus as proof.
10. **Lean optionality.** Advanced capabilities remain physically optional and idle when
    disabled; capability growth must not make the privileged core opaque or the coordinator
    permanently heavy.
11. **Adapter-free recovery.** Core can verify its release identity, replay canonical state and
    expose owner status when every provider, external harness, plugin and frontend is absent.

## Primary workflows

- Configure providers, authenticated CLIs, API routes, capacity pools, models, nodes, and policies through files or the CLI.
- Use Grill Me to convert vague intent into a versioned, accepted GoalSpec.
- Let the Recursion Head start and monitor one or more bounded ARM executives.
- Talk directly to an ARM through its addressable topic without relaying every exchange through
  Head; Head remains aware through canonical state.
- Optionally place multiple named ARMs in a shared conversation or workflow. Head retains the
  overall goal, membership, authority, conflicts and lifecycle while one named ARM may hold a
  bounded workflow-step coordination lease.
- Allow each ARM to delegate research, implementation, testing, review, and integration within inherited authority.
- Move execution between approved models and pools at safe checkpoints while retaining logical identity and evidence.
- Observe progress, queue causes, budget state, route decisions, context health, and blockers from stable read-only interfaces.
- Open, close and rearrange terminal views without changing actor or process lifetime; terminate
  work only through explicit policy-aware commands.
- Produce a verified review branch for the owner; never merge or deploy autonomously by default.
- Run a research GoalSpec that discovers scholarly/technical evidence, builds a claim ledger,
  seeks disconfirmation and produces a reproducible cited decision packet.

## Optimization target

OR-024 refines OR-017 with responsiveness, subscription efficiency and task completion as the
default ranking. Preserve the complete optimization vector behind non-tradeable
authority, safety, privacy, effect-correctness and quality floors. It includes verified quality,
recovery/replay, owner review, quota/tokens/money, context/cache efficiency, latency/throughput,
host resources, terminal interaction, portability/configurability, code/maintenance surface and
extensibility/idle cost. No scalar score hides a regression. Named profiles may select a Pareto
point only through explicit owner-visible policy. The canonical measurements and decision rule
are in [PERFORMANCE-PLAN.md](PERFORMANCE-PLAN.md#canonical-optimization-decision-rule).

## Non-goals for the initial v3 release

- Reselling inference or becoming a mandatory hosted gateway.
- Providing a mandatory central plugin marketplace or vendor-controlled authorization service.
- Requiring use of the opt-in GUI or mobile clients.
- Guaranteeing that unrelated providers share server-side prompt/KV caches.
- Persisting hidden chain of thought or treating it as required recovery state.
- Reimplementing Git, provider-native authentication, or every provider's optimized inner agent
  loop. Recursion integrates and routes across them; it does not reimplement each vendor loop.
- Making protected-branch merge, production deployment, credential administration or authority
  expansion implicit/default consequences of ordinary autonomy. A future explicit Full Access
  grant may include supported high-impact capabilities only after constitutional reconciliation.
- Running every worker on the coordinator VPS.
- Exporting private prompts, source, telemetry, or project evidence by default.

Amendment proposed 2026-10-24: "Requiring use of the opt-in GUI or mobile clients" is
already the D-038 floor (CLI-only use remains valid). This amendment does not change that.
See Surfaces above for what it adds beyond D-038.

## Release philosophy

V3 is one monumental product release built as independently testable internal milestones. A public `v3.0.0` tag should wait until the mandatory end-to-end contract passes, while core, standard, remote, full and custom release manifests control installed surface area. Future product defaults proposed after constitutional reconciliation include `checkpoint` PAUSE, trusted bounded autonomy, digest-aware plugin grants and detachable terminals. D-022 fixes one pause/stop vocabulary (Aggressive / Full / Lazy plus checkpoint / interrupt / drain); installing `full` never implies maximum `sovereign` authority. This is an atomic product promise with incremental construction, not a big-bang implementation.

## What must be proven

- A strong ARM plus deliberate delegation produces better accepted code per quota unit than uncontrolled swarms.
- Semantic context artifacts and compaction reduce repeated input without losing owner intent, constraints, or evidence.
- The coordinator stays responsive on the target VPS under realistic event, store, plugin and monitoring load. (No store is selected; SQLite is one unrun challenger.)
- Users can understand and change routing without reading source or invoking a model.
- Provider and node failures cannot silently widen spend, privacy exposure, or permissions.
- V3's additional machinery produces measurable benefit over the accepted v2 baseline.
- Direct ARM topics and multi-ARM collaboration reduce owner coordination effort without
  creating conflicting authority, instruction loss or an AI ARM Manager.
- Scientific Research ARM output withstands false-citation, outdated-source, contradiction,
  reproducibility, copyright, privacy and independent-source-verification gates.
