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
Lucas-authored; **Lucas accepts it**. Subsequent spec changes by Samir or Dan (or their
attributed agents) are accepted when a founder other than the author approves the PR.

## How to read this

- **Accepted** text in `docs/v3/*`, OR-021…OR-039, and D-017…D-043 remains in force until
  Lucas accepts this amendment.
- **Proposed** text below is the 2026-10-24 north star. D-038 (accepted 2026-10-02) already
  superseded older "GUI later" first-release exclusions. This proposal does not reopen
  D-038. It adds the Tauri/daemon-served client, standalone app and "goat" bar. Other
  older ceilings (agent-never-edits-settings as the only operator path) get dated
  addenda.
- **Research questions** (F-1…F-14 and the others named here) are questions, not decisions.
- Evidence classes stay distinct: source-documented, locally observed, inferred, unmeasured.

## Research evidence

Research for this amendment lives in `CSL-Laboratories/csl-research`.

- **Repository:** `CSL-Laboratories/csl-research`
- **Commit:** `250fdbbe2964e4889096a487e2dfb6d7794046a6`
- **Path:** `submissions/20261025-recursion-csl494-rp0-amendment-research-01/`
- **Claim:** RP0 amendment research grounding, F-1…F-15, and the parent-keyed register for CSL-494.
- **Limitation:** candidate evidence; vault PR https://github.com/CSL-Laboratories/csl-research/pull/41 is pending review. Not human product acceptance.

Product files only link here. This amendment does **not** assert product names for
Anthropic's or OpenAI's agent/bot operators (F-15).

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

D-038 (accepted 2026-10-02) already made GUI and mobile **built but opt-in** in the first
release; no capability is deferred by the old "GUI later" exclusion wording. F-2 is
answered by D-038. This amendment does not reopen that. **Beyond D-038** it adds: a
daemon-served web GUI in a thin Tauri shell on Linux, Windows and macOS; a standalone
app; and the "goat" bar (seven-point bar, bar 4). Requiring the GUI (forbidding CLI-only
use) remains a non-goal. Historical "GUI later" sentences in live spec files are kept;
dated pointers send readers here. `ANCHOR.md` still contains "terminal first, GUI later"
and is **not edited** (protected path; no founder authoring session). That parenthetical
is already superseded by D-038; the additions beyond D-038 live in this file.

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

F-IDs used in this spec: F-1 Octopus; F-2 GUI (answered by D-038); F-3 language/runtime;
F-4 enterprise; F-5 Anthropic unattended usage; F-6 Devin/Cursor keys; F-7 Grok Bot API;
F-8 Task Capsule; F-9 default relay; F-10 durable execution vs replay; F-11 remote steering;
F-12 native vs PWA; F-13 preference sync; F-14 agent step-up; F-15 Anthropic/OpenAI
agent/bot products (working labels only). Full questions, sources and the parent-keyed
register are in the research-evidence block above. Lucas can decide any of these at any
time; silence is not a decision.

## 5. Files this proposal amends (additions only)

Historical research-lane snapshots are **not** rewritten. Each live spec file that
contradicts the direction gets a dated pointer and a local addition.

| File | Direct contradiction | Change in this PR |
|---|---|---|
| [PRODUCT.md](PRODUCT.md) | Status; initial-user "agent never required to edit settings" as a ceiling | Front-matter + new section; OR-038 paragraph **untouched**; vault evidence link. D-038 already in force; this section adds Tauri/daemon/standalone/"goat" |
| [INTERFACE-STRATEGY.md](INTERFACE-STRATEGY.md) | Historical non-goal "No graphical frontend in the initial release" (already superseded by D-038) | Dated pointer: D-038 in force; this amendment adds Tauri/daemon/standalone/"goat". Original line kept |
| [ROUTE-CAPABILITIES.md](ROUTE-CAPABILITIES.md) | BYOK "native loop remains to be built"; missing Grok Bot / Devin / OpenCode Go/Zen rows | Add proposed overlay rows. Claude Bard / Codex Dots marked as Lucas's working labels, unverified, F-15 |
| [INTERACTION-SURFACES.md](INTERACTION-SURFACES.md) | Historical "requires no web, desktop or mobile graphical client" (already superseded by D-038) | Dated pointer; adds Tauri/daemon/standalone beyond D-038 |
| [CLI.md](CLI.md) | Historical "No graphical frontend is required for the v3 release" (already superseded by D-038) | Dated pointer; adds Tauri/daemon client beyond D-038 |
| [TERMINAL-RUNTIME.md](TERMINAL-RUNTIME.md) | Historical "A later graphical frontend" (already superseded by D-038) | Dated pointer: D-038 in force; this amendment adds the daemon-served Tauri client on the same protocols |
| [START-HERE.md](START-HERE.md) | Historical "graphical application follows" (already superseded by D-038) | Dated pointer; keep historical sentence |
| [DECISION-REGISTER.md](DECISION-REGISTER.md) | OR-021/OR-025 GUI-later wording (already superseded by D-038) | Dated pointer; **OR rows not rewritten** |
| [ROADMAP.md](ROADMAP.md) | Historical "graphical/media expansion remain deferred" (GUI already in-scope under D-038) | Dated pointer; hosted tenancy stays research (F-4) |
| [docs/v3/README.md](README.md) | Historical "The graphical application is later" (already superseded by D-038) | Dated pointer |
| [ANCHOR.md](../../ANCHOR.md) | Historical "a client (terminal first, GUI later)" (already superseded by D-038) | **No edit.** Protected path; no founder authoring session. Supersession carried in this file only |
| [README.md](../../README.md) | Governance note claims CONTROL_PLANE/ANCHOR "still describe the pre-v3 control plane" | Correct the stale note. Does not edit ANCHOR.md |
| [CONTROL_PLANE.md](../../CONTROL_PLANE.md) | **No direct contradiction** (already v3-only, post D-017) | **No edit.** Listed so the PR inventory is complete. |

`state/DECISIONS.md` is not edited. D-038 (accepted 2026-10-02) already made GUI and mobile
**built but opt-in** in the first release; no capability is deferred by the old exclusion
wording. This amendment does not reopen D-038. It adds a daemon-served client in a thin
Tauri shell on Linux, Windows and macOS, a standalone app, and the "goat" bar. Requiring
the GUI remains a non-goal.

## 6. What this does not do

- Select a product language, store, framework, Temporal vs replay kernel, or default relay.
- Authorize live spend, credentials, quota resets, or company-server deployment.
- Rewrite OR-038/OR-039 or any other exact Owner-approved paragraph.
- Admit RP1–RP16 implementation work. Those remain separate issues under the Nov 11 umbrella.
- Touch workflow files, CI, or Devin settings.
- Keep a living research register in this repository. Research lives in
  `CSL-Laboratories/csl-research` (see Research evidence).
