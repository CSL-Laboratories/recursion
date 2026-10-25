---
title: Recursion v3 spec amendment proposed 2026-10-24
status: amendment proposed (2026-10-24); acceptance is a human decision by Lucas
last-updated: 2026-10-25
authority: proposed; does not admit work, clear a hold, select a language/store/framework, authorize spend, or decide product goals
---

# Recursion v3 spec amendment proposed 2026-10-24

**Status: amendment proposed (2026-10-24); acceptance is a human decision by Lucas.**

This file **adds** proposed product direction. It does not rewrite Owner-approved wording.
The OR-038 paragraph in [PRODUCT.md](PRODUCT.md) is preserved verbatim. `state/DECISIONS.md`
is not edited. Publishing this proposal does not mark the issue decided, does not clear a
hold, and does not authorize implementation.

Agent (Cursor Cloud `bc-5c609d8d-1982-5753-a90d-8974cde61752`, for Lucas): drafted from
Lucas's 2026-10-24 direction and the graded competitive brief. Linear issue
[CSL-494](https://linear.app/csl-laboratories/issue/CSL-494) (RP0), umbrella
[CSL-493](https://linear.app/csl-laboratories/issue/CSL-493). This RP0 amendment is
Lucas-authored; **Lucas accepts it**. Subsequent spec changes by Samir or Dan follow
[RESEARCH-REGISTER.md](RESEARCH-REGISTER.md) (another founder's PR approval counts as
acceptance).

## How to read this

- **Accepted** text in `docs/v3/*`, OR-021…OR-039, and D-017…D-043 remains in force until
  Lucas accepts this amendment.
- **Proposed** text below is the 2026-10-24 north star. Where it conflicts with older
  first-release exclusions (GUI later, CLI-only surfaces, agent-never-edits-settings as a
  ceiling), this proposal is the intended supersession.
- **Research questions** (F-1…F-14 and the others named here) are questions, not decisions.
- Evidence classes stay distinct: source-documented, locally observed, inferred, unmeasured.

## Research grounding (this session)

| Source | Grade | Use |
|---|---|---|
| `CSL-Laboratories/recursion` master `954ddc607f6b8736a09ebf5aed872fce8355ce3e` | locally observed | Spec baseline |
| Graded brief *Recursion: competitive frontier and pillar design brief* (Grok Bot, preparing research for Lucas, 2026-10-24) | candidate research; citations [R#]/[S#] as in that brief | Pillars, competitor matrix, gaps, F-questions |
| *Recursion Frontier Map* (Grok Bot, preparing research for Lucas, 2026-10-25) | candidate research | Seven-point bar, Nov 11 horizon, eval non-goals |
| Lucas task text 2026-10-25 (this PR) | attributed direction for Lucas to accept | North star, surfaces, operators, spend, method |
| [CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research) | **not readable this session** (GitHub API 404) | No vault record written or cited |

No pay-per-token research run was made. Exa MCP returned HTTP 429 / free-tier limit this
session. This amendment does **not** assert product names for Anthropic's or OpenAI's
agent/bot operators (F-15). Lucas (2026-10-24 11:53pm PT): "Claude has their own bot, not
sure what it's called."

## 1. North star (proposed)

Recursion is **the frontier-defining harness** — "the goat": a first-class native harness
**and** a first-class unifying wrapper that is meant to beat any harness in the way,
including Octopus (which Octopus is F-1), the DeepSeek harness
(`deepseek-ai/deepseek-harness`; see brief §1), Hermes Agent, T3 Code, OpenCode, Claude Code,
Codex, and more.

This does not replace the accepted public thesis **"the harness that scales with your
workflow"** (D-034, [PRODUCT.md](PRODUCT.md) Thesis, [ANCHOR.md](../../ANCHOR.md)). It
states the competitive bar those contracts must now earn by 11 Nov 2026.

Ambition is not shrunk to fit the calendar. Nov 11 is everything that can fit. A public
demo is required. A private held-out eval is allowed. No SWE-bench chasing. Linux-first
sprint for sandboxes and nodes; product GUIs target Linux, Windows and macOS.

## 2. Lucas's seven-point bar (proposed gate on every parent)

From the 2026-10-25 frontier map, scored against the landable form, not a later vision.
A candidate that "will add the GUI later" **fails bar 4**.

| # | Bar | Meet (proposed) |
|---|---|---|
| 1 | Not AI slop: researched and developed, with evidence | Frozen oracles, independent rerun, named baselines, unknown ≠ pass |
| 2 | High quality, practical for enterprise and developers | Review-branch publish, no credential custody, truthful Stop, audit receipts; SSO/SCIM are research (F-4), not fakes |
| 3 | Hyper-optimized | OR-024 ranking visible; attach/open GUI launches zero inference; disabled-client idle |
| 4 | A real graphical interface users interact with | Journeys completed in a GUI over the same command/query daemon as the CLI; closing the window does not kill work |
| 5 | Directed and installed by agents | Machine-readable setup + Control MCP so Grok Bot and Anthropic's and OpenAI's own agent/bot products (working labels: Claude Bard, Codex Dots; exact products are F-15) can finish install without a human reading docs |
| 6 | Config syncs with providers/preferences users already use | Import/observe Claude, Codex, Cursor, git, `AGENTS.md`; never copy tokens |
| 7 | Loved features, optimized and done correctly | Plan, diffs, worktrees, Stop, fork, approvals, PR-as-publish, honest usage — under Recursion contracts (generated ≠ accepted, unknown ≠ 0, MCP ≠ grant, window-close ≠ Stop) |

## 3. Core pillars (proposed)

Each pillar extends an existing spec section. Design while researching; develop only once
grounded; then test. Three phases with gates (brief §7 / frontier map §5.3):

1. **Grounding** (design + spikes, citations, tests named).
2. **Development** (only after G1).
3. **Testing** (head-to-heads, journeys, independent rerun of a frozen SHA).

Live `--allow-live` Codex and Claude Code runs go on **subscriptions**, with **no dollar
spend limit**, usage tracked against quotas. **No pay-per-token for CSL.**

### P1 — Calling and piloting other subscriptions

Drive the user's own authenticated CLIs and APIs (Claude Max, Codex, Cursor, Devin, Grok Bot,
OpenCode Go/Zen, and the seven launch-critical names). Three-tier Route Adapter SDK
(structured / stream / supervised PTY). Per-route compliance card. No credential custody
(D-021/D-029). Extends [ROUTE-CAPABILITIES.md](ROUTE-CAPABILITIES.md), D-021/D-023/D-030.

### P2 — Cross-chat

Provider-neutral Task Capsule; inbox with accept/hold/refuse; messages never count as
consent; Head-coordinated Projects with threads on any route or node. Extends
[CONTEXT.md](CONTEXT.md), D-024. Smallest capsule is F-8.

### P3 — Remote control

One authenticated Control API (JSON-RPC over WS/HTTP + observer stream) for TUI, GUI,
mobile/PWA, Telegram and agents. QR pairing. Optional E2E relay. Remote listeners never
unauthenticated. Default relay is F-9. Extends [INTERACTION-SURFACES.md](INTERACTION-SURFACES.md),
[SECURITY.md](SECURITY.md), D-036/D-038.

### P4 — Enterprise

Self-hosted team capabilities: SSO/RBAC, one org policy compiled onto every route, audit,
secrets-by-reference, observability, tenant isolation. What "enterprise" means by Nov 11
is F-4; hosted multi-tenancy stays a research/design question, not a fake claim.
Extends [SECURITY.md](SECURITY.md), [PRODUCT.md](PRODUCT.md) Audience.

### P5 — Node orchestration

Leased, fenced workers across machines (sentry, Hetzner, cloud). `recursion node` enrollment,
signed work packets, placement by capability/egress/quota. Durable execution vs the in-house
replay kernel is F-10. Extends [EXECUTION-FABRIC.md](EXECUTION-FABRIC.md), D-020.

### P6 — Surfaces (resolves the v1 no-GUI contradiction)

One headless daemon. Clients:

- a **daemon-served web GUI** in a **thin Tauri shell** for Linux, Windows and macOS
  (Lucas R-Q4; Tauri is the GUI-shell direction, **not** a product-language selection —
  OR-017 / D-039 remain);
- a **standalone app**;
- a **CLI/TUI**;
- phone/web remote clients (pairing; native vs PWA is F-12).

The GUI is a **first-release client**, not a later milestone. It should be the most
polished visual surface, refined daily. Requiring the GUI (forbidding CLI-only use) remains
a non-goal. Extends [INTERFACE-STRATEGY.md](INTERFACE-STRATEGY.md); supersedes the
"No graphical frontend in the initial release" non-goal (F-2, answered by R-Q4).

### P7 — BYOK and preference sync

BYOK keys live in the **OS keyring**, never in repos, preference bundles or agent context.
**First providers: OpenRouter, OpenCode Go, OpenCode Zen**; more compatibility ships later
(Lucas N-3, 2026-10-24 11:19 PM PT). Signed preferences bundle syncs routing, presets,
skills, memory policy and provider descriptors — never secrets. Sync backend is F-13.

### P8 — Agent-installable and agent-operable

Recursion must be operable by agent software via **MCP or a control API**, explicitly
including Grok Bot (inbound driver API still unverified, F-7; interim: Grok Bot is the
*caller* of Recursion's Control MCP) and Anthropic's and OpenAI's own agent/bot products
(Lucas's working labels: Claude Bard, Codex Dots); exact products and their control
surfaces are a RESEARCH question, decide from findings (F-15). This amendment does not
assert product names for those labels.

Installer (`curl \| sh` / `npx` / `pipx`) plus `llms.txt` and `AGENTS.md`. Control MCP and
ACP-agent face expose `doctor`, `status`, `task`, `route explain`, `stop`, `approval` with
capability tokens. A2A Agent Card. Owner step-up vs self-approval is F-14.

### P9 — Verification and review

Independent cross-family verifier lanes with independence-vector receipts. GUI diff review.
Implementer PASS is never acceptance.

### P10 — Quota and cost ledger

One ledger over plan windows, credits and BYOK spend. Route on remaining headroom.
Model-free `budget show` / `route explain`. Unknown stays unknown.

### P11 — Memory and learning

Learned skills enter as proposals with provenance and review, then become plugins.
`agentskills.io` compatibility. Learned skills stay out of the privileged core.

### P12 — Plugin and skill portability

Import Cordis, OpenCode, Goose recipes, MCP, Claude skills/plugins. Report lossy behavior.

### P13 — Automation and events

Cron, GitHub/Linear/Slack triggers, CI-failure reactions as **bounded goals**. Must not
become a standing executive ([ANCHOR.md](../../ANCHOR.md)).

### P14 — Security and sandboxing

Per-lane sandboxes, egress broker, E2E remote, supply-chain pinning. Linux-first cgroup v2;
Windows floor parity remains D-028.

### P15 — Observability and replay

Event-log replay, OTel export, a model-free "why" explainer for every route and Stop.

### P16 — Research mode

Cited research GoalSpec with a claim ledger. Bounded fan-out. D-025 bounds remain.

### P17 — Evaluation

Private held-out eval is allowed. No SWE-bench chasing. Public demo is required by 11 Nov 2026.
Head-to-heads against named harnesses (Claude Code, Codex, OpenCode, T3 Code, DeepSeek harness,
Octopus candidates, Hermes Agent) are Phase 3 gates on the parent issues, not a public
leaderboard. Independent rerun of a frozen SHA is the closeout; implementer PASS is never
acceptance.

## 4. Research questions, not decisions

Copied from the attached Page (F-1…F-14). Status `RESEARCH` unless marked answered.
Lucas can decide any of these at any time; silence is not a decision.

The living parent-keyed index is [RESEARCH-REGISTER.md](RESEARCH-REGISTER.md) (Lucas
2026-10-24 11:57pm PT: the spec must be dynamic). Publishing a csl-research record
updates that register in a docs-only PR. PRODUCT.md accepted text changes only under
that register's update rule: Lucas-authored dated amendments are accepted by Lucas;
spec changes by Samir or Dan (or their attributed agents) are accepted when a founder
other than the author approves the PR (Lucas 2026-10-24 11:58pm PT).

| ID | Question | Status |
|---|---|---|
| F-1 | Which "Octopus" did Lucas mean? Beat both until named: `archeryue/Octopus` (personal agent platform) and `nyldn/claude-octopus` (12-provider consensus plugin). | RESEARCH |
| F-2 | Spec GUI contradiction vs D-038 | **ANSWERED** by R-Q4 (this amendment) |
| F-3 | Product language and runtime (OR-017) | RESEARCH |
| F-4 | What "enterprise" means by Nov 11 (self-hosted team vs hosted multi-tenant) | RESEARCH |
| F-5 | Anthropic "ordinary, individual usage" for unattended `claude -p` fan-out (D-027 counsel) | RESEARCH |
| F-6 | Devin/Cursor API keys vs D-021 no-custody (keys by OS-keyring reference) | RESEARCH |
| F-7 | Grok Bot inbound/driver API | RESEARCH (unverified) |
| F-8 | Smallest Task Capsule that keeps fidelity across model families | RESEARCH |
| F-9 | Default remote relay: self-hosted E2E vs Tailscale vs hosted (no mandatory hosted gateway) | RESEARCH |
| F-10 | Temporal-style durable execution vs in-house replay kernel (PR #292 / CSL-336) | RESEARCH |
| F-11 | Remote steering: vendor remotes vs always via Recursion | RESEARCH |
| F-12 | Native mobile vs PWA by Nov 11; D-036 online-only mobile view | RESEARCH |
| F-13 | Preference sync backend (Git vs CRDT vs coordinator store) and offline conflict model | RESEARCH |
| F-14 | Agent-operator step-up list and prevention of agent self-approval | RESEARCH |
| F-15 | Anthropic's and OpenAI's own agent/bot products (Lucas's working labels: Claude Bard, Codex Dots); exact products and their control surfaces are a RESEARCH question, decide from findings. Lucas (2026-10-24 11:53pm PT): "Claude has their own bot, not sure what it's called." Do not assert product names. | RESEARCH |

Also still research (not decided here): the product language (F-3), which Octopus (F-1),
the default remote relay (F-9), durable execution vs replay (F-10), and F-15.

## 5. Files this proposal amends (additions only)

Historical research-lane snapshots are **not** rewritten. Each live spec file that
contradicts the direction gets a dated pointer and a local addition.

| File | Direct contradiction | Change in this PR |
|---|---|---|
| [RESEARCH-REGISTER.md](RESEARCH-REGISTER.md) | Spec treated as static vs Linear parents and published research | **New** living register; docs-only updates; PRODUCT.md accepted text follows the register update rule (Lucas-authored vs Samir/Dan other-founder approval) |
| [PRODUCT.md](PRODUCT.md) | Status; initial-user "agent never required to edit settings" as a ceiling; GUI treated as opt-in-later in places | Front-matter + new section; OR-038 paragraph **untouched**; link to the living register |
| [INTERFACE-STRATEGY.md](INTERFACE-STRATEGY.md) | Non-goal "No graphical frontend in the initial release" | Add supersession note; keep original line |
| [ROUTE-CAPABILITIES.md](ROUTE-CAPABILITIES.md) | BYOK "native loop remains to be built"; missing Grok Bot / Devin / OpenCode Go/Zen rows | Add proposed overlay rows |
| [INTERACTION-SURFACES.md](INTERACTION-SURFACES.md) | "v3 release requires no web, desktop or mobile graphical client" | Add supersession note |
| [CLI.md](CLI.md) | "No graphical frontend is required for the v3 release" | Add supersession note |
| [TERMINAL-RUNTIME.md](TERMINAL-RUNTIME.md) | "A later graphical frontend" | Add that GUI is a same-protocol first-release client |
| [START-HERE.md](START-HERE.md) | "The dedicated graphical application follows the initial terminal platform" | Add pointer; keep historical sentence |
| [DECISION-REGISTER.md](DECISION-REGISTER.md) | OR-021 "terminal first, dedicated GUI later"; OR-025 "dedicated GUI have separate later milestones" | Add proposed-supersession note; **OR rows not rewritten** |
| [ROADMAP.md](ROADMAP.md) | "graphical/media expansion remain deferred"; M6 CLI-only | Add pointer |
| [docs/v3/README.md](README.md) | "The graphical application is later" | Add pointer |
| [ANCHOR.md](../../ANCHOR.md) | "a client (terminal first, GUI later)" | Add dated amendment; mission paragraph otherwise kept |
| [README.md](../../README.md) | Governance note claims CONTROL_PLANE/ANCHOR "still describe the pre-v3 control plane" | Correct the stale note |
| [CONTROL_PLANE.md](../../CONTROL_PLANE.md) | **No direct contradiction** (already v3-only, post D-017) | **No edit.** Listed so the PR inventory is complete. |

`state/DECISIONS.md` is not edited. D-038 (GUI/mobile built but opt-in) remains the last
accepted decision; this proposal asks Lucas to raise GUI from opt-in-later packaging to a
**first-release client** while keeping "must use the GUI" a non-goal.

## 6. What this does not do

- Select a product language, store, framework, Temporal vs replay kernel, or default relay.
- Authorize live spend, credentials, quota resets, or company-server deployment.
- Rewrite OR-038/OR-039 or any other exact Owner-approved paragraph.
- Admit RP1–RP16 implementation work. Those remain separate issues under the Nov 11 umbrella.
- Touch workflow files, CI, or Devin settings.

## 7. Sources (abbreviated)

Full graded source tables are in the competitive brief §8. Recursion pins at
`954ddc607f6b8736a09ebf5aed872fce8355ce3e`.

External identifications used in this amendment (retrieval 2026-10-24 or 2026-10-25):

- DeepSeek Harness: https://github.com/deepseek-ai/deepseek-harness
- Octopus candidates: https://github.com/archeryue/Octopus · https://github.com/nyldn/claude-octopus
- Grok Bot: https://docs.x.ai/grok-bot/overview
- T3 Code: https://t3.codes/ · https://github.com/pingdotgg/t3code
- OpenCode Go: https://opencode.ai/docs/go/
- Claude Code remote control / Projects / legal: https://code.claude.com/docs/en/remote-control · https://code.claude.com/docs/en/claude-projects · https://code.claude.com/docs/en/legal-and-compliance
- Codex App Server: https://developers.openai.com/codex/app-server.md
- Happy E2E relay: https://github.com/slopus/happy
- Replay kernel: recursion PR #292 / CSL-336
