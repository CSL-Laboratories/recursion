# Recursion — architecture

Last updated: 2026-08-31. Read `ANCHOR.md` first — that's WHY/WHAT; read `CONTROL_PLANE.md` for
WHO decides; this is HOW.

<!-- superseded-by: CONTROL_PLANE.md (2026-08-06, D-010) — authority claims only -->

> **AUTHORITY SUPERSEDED 2026-08-06 (D-010).** Two claims below are no longer the model:
> **Hermes is not a "front/router"** or message bus — it is the executive control plane, top of
> the automation chain, holding the portfolio and the goal→task→event lifecycle. And **Claude is
> not "the conductor session you drop into"** — it is a bounded executor that receives one task,
> one worktree, allowed paths, explicit non-goals, and a machine-checkable gate. Deep steering
> happens with Hermes. See `CONTROL_PLANE.md`.
>
> Everything else here — the tiering, the escalation scoring, the cost model, the where-it-runs
> map, the context-management rules — is current and load-bearing.

> **V2 SOURCE-ONLY MVP ACCEPTED — 2026-08-31.** The provider-neutral contracts, packet kernel,
> adapters, placement/durability fabric, bounded orchestrator, Telegram command semantics, and
> watchdog source have passed the one-command MVP gate and independent review. The engine roster
> below still describes the deployed v1 lineage until Lucas publishes, merges, provisions the
> production seams, deploys, and completes a live soak. See `docs/recursion-v2-mvp-harness.md`,
> `docs/plans/recursion-v2-execution-fabric.md`, and the completion audit. Source acceptance does
> not change the authority hierarchy, PAUSE, review-only publication, or human deployment boundary.

## Purpose

One control plane over several workstreams that each move at a different tempo and need a different
amount of Lucas. The bottleneck being solved is **context-switching**: juggling a separate
session/terminal per project is itself what slows development and learning. Recursion is the single
front + shared brain; the workstreams report into it, it routes work out.

## Workstreams and interaction intensity

| Workstream | Runs on | Interaction | Autonomy ceiling | Why |
|---|---|---|---|---|
| Fleet ops (vend/disco/holla) | gs-worker-nbg + operator API | background | medium | auto-fix reversible; escalate costly/risky; mining = DO-NOT-DEPLOY |
| Supervision + routing | gs-recursion-nbg (always-on) | background | — | the dispatcher + ledger + alerting |

**Key principle: autonomy scales with machine-verifiability.** A workstream with a ground-truth
oracle is self-reviewing and can't create the unreviewable backlog ANCHOR warns about. Fleet work is
partially checkable → auto the safe part, escalate the rest. Background workstreams then run
concurrently: the fleet self-heals while the rest of the work proceeds, all reporting into one ledger.

## Recursion v2 harness — accepted source architecture

The v2 harness turns a human-authored goal into a bounded, independently verifiable review
candidate without allowing a model or provider to acquire control-plane authority:

```text
Lucas instruction
  → Hermes decision and bounded task contract
  → immutable TaskPacket and child budget
  → capability/health/quota negotiation
  → deterministic host + pool + sandbox placement
  → durable lease, attempt, result, and recovery journal
  → distinct verifier task + authenticated verification receipt
  → review intent + repository-approved publisher
  → Lucas review, merge, deployment, and production credentials
```

The implementation is split into six source boundaries:

| Boundary | Responsibility |
|---|---|
| `mvp_contracts.py` | strict versioned records, canonical digests, cross-record and child-budget validation |
| `mvp_kernel.py` | immutable depth-2 packets, context retention, issuance and lineage authority |
| `mvp_adapters.py` | capability negotiation, exact dispatch plans, result/evidence normalization and deduplication |
| `execution_fabric.py` | host/pool placement, durable leases/results, restart recovery, receipts and integration serialization |
| `mvp_orchestrator.py` | packet admission, aggregate budgets, implementer/verifier separation, review-intent admission |
| `mvp_control_surface.py` | fresh snapshot queries, authenticated control intents, and signed controller-death detection |

All six are inert libraries at the source boundary: they do not themselves contact providers,
Telegram, mutate PAUSE, deploy, merge, or reveal credentials. External effects remain in existing
authenticated transport/actuator/publisher layers. The exact feature matrix and limitations are
in `docs/recursion-v2-mvp-harness.md`.

## Engine: a bounded escalation-tiered execution harness

The always-on component is deliberately light and event-driven — mostly idle, wakes on a trigger
(cron, fleet alert, a message from Lucas). It does not run a big model non-stop. Deterministic
source code validates authority, budget, capacity, evidence, and publication; models are replaceable
bounded workers behind that harness. **This is what makes a 24/7 brain fit the quota.**

Tiers (names are a skin over the function — rename freely):

- **Lua — routine heartbeat.** Cheap, frequent health/status checkins (fleet alive? queue draining?
  build loop progressing? anything new in the ledger?). Runs on the cheapest tier (local rules /
  OpenRouter). Escalates up only on a real signal.
- **Terra & Sol — decision agents (mid-tier reasoning).** Fire only when Lua surfaces something.
  - **Terra** — infrastructure/fleet: keep workers alive, refill queues, replace jailed accounts
    within guardrails, "build/repair infra" moves.
  - **Sol** — keep the productive loop going: iteration direction and scheduling.
  - Run on `claude -p` (Claude subscription, flat) for hard reasoning, or GPT/Codex, or cheap
    OpenRouter per `routing.yaml`.
- **Hermes — the front/router.** The single interface Lucas talks to + the message bus between tiers
  and Lucas. Async (INBOX/Telegram) and sync (a conductor session).
  <!-- superseded-by: CONTROL_PLANE.md (2026-08-06, D-010) -->
  > **SUPERSEDED.** Hermes is the **executive control plane**, not a router: it holds the portfolio
  > and the goal→task→event lifecycle, decides what runs next, monitors it, and takes direct orders
  > from Lucas. *Still true:* it remains the single interface Lucas talks to — that part of the
  > original design was right and is why it became the executive rather than being replaced.
- **Lucas — top of the escalation chain.** Sees only what genuinely needs him (below).

### Engines in scope + control — the loop runs on Hermes (verified 2026-08-04)

<!-- superseded-by: CONTROL_PLANE.md (2026-08-06, D-010): "front/router" below is the old framing.
     The harness fact in this section — Hermes on Codex OAuth — is unchanged and current. -->

The always-on loop runs on **Hermes** (`NousResearch/hermes-agent`) as the harness — the **Hermes**
tier (front/router) made concrete. Hermes supports OpenAI **Codex OAuth**, so the loop runs on
Lucas's **ChatGPT Pro subscription** (GPT-5.x via Codex OAuth) — NOT metered API, NOT Claude — with
an **automated fallback to OpenRouter** on quota exhaustion / rate-limit / error.

- **Primary: ChatGPT Pro via Codex OAuth** — the 24/7 loop engine. Subsidized by the $200 sub; Pro's
  high limits suit an always-on agent. Interactive OAuth login (Lucas does it once on the box).
- **Fallback: OpenRouter** — Hermes swaps mid-session via `fallback_providers`; `provider.sort=price`.
- **Optional heavy tier: Claude `claude -p`** — flat-cost, added only if a specific task wants it.
  Deliberately NOT in the 24/7 path — avoids burning the soon-halved Claude weekly limit round-the-clock.

**Control (Lucas owns it):**
- Lucas owns the Hermes provider/fallback config and interacts through Hermes' own management surface
  (the single front). The loop never edits its own routing/auth/budget (CLAUDE.md hard rule 3).
- ⚠ **Automatic fallback can silently revert to *metered* OpenRouter billing.** Guard: alert when
  fallback fires (`routing.yaml` `fallback_threshold_per_day: 3 → notify`) so silent spend can't hide.
- Residual risk: leans on OpenAI continuing to permit Codex OAuth in third-party harnesses. If that
  changes, the loop degrades to OpenRouter automatically — the fallback chain absorbs it, no redesign.

> ⚠ Corrects an earlier claim in this session that the ChatGPT sub "can't power headless automation."
> That is OpenAI's *general* API-key guidance; the **Codex-OAuth-in-Hermes** path is the exception,
> and it is what we use. See D-006.

## The model roster — who's who, who calls whom, what it costs

Five model names are in play. They are NOT five peers — they fall into two planes, and only one
plane is metered. This is the whole interaction map: if it holds, everything interacts as expected.

**Plane 1 — the supervision brain (flat-rate, always-on).** The GPT 5.6 family on the ChatGPT Pro
sub via Codex OAuth: three capability tiers, one sub, ~$0 at the margin.

| Model | Tier / job | Fires when |
|---|---|---|
| GPT 5.6 Luna  | cheap heartbeat / triage (`heartbeat`) | every cron tick — "anything wrong or new?" |
| GPT 5.6 Terra | brief + routine decisions (`brief`/`decision`, the DEFAULT) | Luna surfaces a signal |
| GPT 5.6 Sol   | escalated hard reasoning (`hard`), pinned per-job | Terra hits a genuinely hard call |

Naming: the Lua/Terra/Sol tier names predate the GPT 5.6 models, which happen to share two of them.
`routing.yaml` now binds them 1:1 (heartbeat→luna, brief/decision→terra, hard→sol), so the "skin"
and the model are the same weight per tier. That is the answer to "it's a lot of models":
Luna/Terra/Sol are ONE family, three sizes, on ONE sub.

**Plane 2 — the execution arms (what the brain dispatches to do the actual work).** The supervisor
reasons; it does not write the renderer. When something needs DOING it spawns an arm:

| Arm | Runs on | Cost | For |
|---|---|---|---|
| Claude Opus (top tier), `claude -p` | Claude Max sub (flat) | ~$0 at margin, kept OFF the 24/7 path (weekly limit) | deep single-focus work — hard multi-file coding and long independent tasks — and the conductor session you drop into |

<!-- superseded-by: CONTROL_PLANE.md (2026-08-06, D-010) -->
> **SUPERSEDED — the Claude row only.** Claude is a **bounded executor**, not "the conductor session
> you drop into": one task, one worktree, allowed paths, explicit non-goals, a machine-checkable
> gate. Deep steering happens with Hermes. *Still true:* Claude is the deep single-focus arm for
> hard independent work and stays off the 24/7 path — that cost and capability reasoning is
> unchanged, and is exactly why it remains the executor of choice for hard work.
| OpenRouter → Deepseek-class, price-sorted | metered ($) | the ONLY per-token spend | (a) bulk PARALLEL cheap coverage; (b) last-resort fallback if Codex OAuth is down |

⚠ "Opus 5": the current top Opus tier is 4.8 (what these sessions run on today). If an Opus 5 ships
it slots into the identical role — the plane, not the version number, is load-bearing.

⚠ Deepseek vs the D-008 fallback chain are two different jobs. The SUPERVISOR's outage fallback is
`luna → gemini-2.5-flash-lite` (D-008, chosen on cost). Deepseek-class is the BULK-EXECUTION lane —
many cheap agents in parallel for coverage-not-correctness work (memory: OpenRouter = coverage).
Both are OpenRouter, both price-sorted, different callers.

**The flow, one line:** cron → Luna heartbeat → (signal) Terra → (hard) Sol → (needs doing) dispatch
to Opus for depth or Deepseek-class for parallel bulk → results to the ledger → Terra briefs you on
Telegram → you steer. Everything that CAN run on a flat sub (Luna/Terra/Sol on Pro; Opus on Max)
does; OpenRouter is fenced to fallback + bulk and watched by `fallback-watch.sh`. That is how the
$200 sub is maximized — the entire 24/7 brain lives inside it.

## Escalation: auto / notify / escalate

Every proposed action is scored on **reversibility × cost × ambiguity × verifiability.** Higher on
any axis → higher it escalates.

- **Auto (act, log only)** — reversible, cheap, machine-verifiable. Oracle-gated iterations that pass.
  Fleet: kick a wedged bot, restart a crashed worker, refill a queue.
- **Notify (act, then tell Lucas async)** — worth knowing, didn't need approval. Committed a branch;
  auto-replaced an account within the jail-plague guardrail. → daily digest + Telegram.
- **Escalate (block on Lucas)** — irreversible / expensive / ambiguous / unverifiable. Provisioning
  accounts (spends money + proxies), major reconfig, merging a workstream to `main`, ANYTHING mining
  (DO-NOT-DEPLOY), a decision not in `DECISIONS.md`, a claim unverifiable against a known
  solution. → `state/INBOX.md` + Telegram ping, and WAIT.

This is the scaffold's "escalate, don't guess" made mechanical.

## How Lucas fits in — the three questions

**1. Escalation to you.** Telegram push (you're often mobile) → the item lives in `state/INBOX.md`
(durable queue). You clear it by answering; the loop reads your answer next cycle. One-page briefs,
prioritized, drop rather than append.

**2. Direct access + changing things.** Four levers:
- **Async (pull, low-attention default):** read INBOX/briefs/state; answer escalations.
- **Sync (drop-in on a PC — the deep-dive escape hatch):** when a Telegram escalation doesn't carry
  enough context to decide, open a **conductor session** — interactive Claude Code (Opus) with full
  read/write to `state/` + dispatch rights. To keep the jump to one step, every escalation carries a
  **context handle**: the workstream id + a pointer to the exact ledger slice / handoff / repo path
  that produced it + the escalation text. On the PC, `recursion open <workstream>` (or paste the
  handle into a Claude Code session in that repo) boots the conductor with that context pre-loaded —
  you land already oriented, not scavenging. Decide; your answer lands in `state/INBOX.md` /
  `DECISIONS.md` and the loop reads it next cycle. This is the review-throughput valve — it lowers
  the cost of getting *enough* context to act.
- **Declarative steering (the wheel):** the loops READ `DECISIONS.md` + `ANCHOR.md`. Change a
  decision → the loops pick it up next cycle. Config as control.
- **Kill switch:** a `state/PAUSE` flag every loop checks first. Present ⇒ autonomous action halts
  instantly; only heartbeat + escalation remain. The safety valve.

**3. Context management (for optimized work)** — the vault already paid for these lessons:
- **The ledger is the shared brain.** `state/` (projects, DECISIONS, gap-map, briefs, INBOX) is the
  durable, compact, cross-agent memory. Agents boot from it, not from a giant transcript.
- **Carry no state.** Every scheduled run RE-DERIVES current state from the ledger + ground truth
  (oracle / fleet-status / DB) at run time. A state block baked into a prompt rots in days and reads
  as working (memory: `handoff-prompts-must-carry-no-state`).
- **Per-workstream working set.** Each loop keeps its own small state (e.g. current gap + last check
  scores; a fleet health snapshot) — not one bloated shared context.
- **Route context depth by tier**, like compute: Lua gets the check only; Terra/Sol get the relevant
  slice; the conductor gets the full ledger on demand.
- **Guard the digest.** Summaries drop quiet-but-important facts (memory: `digests-miss-inert`).
  Periodic full refresh against ground truth, not just rolling summaries.

## Observability — seeing the parallel work

Push tells you what changed; you also need a pull view of what's running RIGHT NOW across the arms.
One source of truth, three ways to read it. The v1 board/log surfaces already exist; the v2
control-surface query semantics are source-accepted but not yet deployed to production Telegram:

- **`state/BOARD.md` — the live registry.** The dispatcher rewrites it each tick: one row per
  in-flight arm — workstream, tier/model, state (queued/running/done/blocked), started, tokens,
  metered $ so far. Committed to `loop/auto`, so it doubles as history. Single source of truth;
  nothing else invents its own status.
- **Telegram `/status` — pull, from your phone.** Ask the supervisor "what's running" → it reads
  BOARD.md and replies. The async counterpart to the pushed brief.
- **On a PC:** `recursion board` prints the same table; `tail -f loop/logs/<date>-*.log` streams any
  single arm live; the conductor session is the deepest view — you are inside the arm.

The board answers the two questions that matter under a flat sub: is any arm silently on metered
OpenRouter? (the $ column, cross-checked by `fallback-watch.sh`) and is generation outrunning review?
(queued/done counts — ANCHOR's actual constraint).

## Where everything runs

- **gs-worker-nbg (Hetzner):** fleet workers (vend/disco/holla) via German residential proxy.
- **gs-recursion-nbg (Hetzner, always-on):** dispatcher (Lua/Terra/Sol/Hermes), state ledger, cron,
  fleet monitor, alerting.
- **Railway (app layer only):** API, Discord bot, dashboard, renderer, Postgres.

## Where the AI compute runs — and what actually costs money

The concern: will the VPS handle it, will traffic get expensive, and does it keep working when your
computer is off? Three separate things, separated.

**Where the arms run: on the VPS, not your computer.** `claude -p`, the OpenRouter calls, and the
Luna/Terra/Sol supervisor are all thin API CLIENTS — the model itself runs in Anthropic / OpenAI /
OpenRouter's cloud. They execute on gs-recursion-nbg and reach those APIs over the network. So the
brain and nearly every arm are 24/7 and do NOT need your computer on. That is the whole reason
Recursion gets its own always-on box (D-I).

**What actually costs money — it is NOT VPS traffic.** Three cost buckets, only one scales:
- *Bandwidth:* negligible and flat. The Hetzner CX box includes ~20 TB/mo egress; AI API calls are
  text (a huge session is a few MB). The VPS bill does not move with AI traffic. Stop worrying about
  this one.
- *Tokens:* the real cost — governed by the sub model, not the VPS. Luna/Terra/Sol on the flat $200
  Pro sub and Opus on the flat Claude Max sub are ~$0 at the margin no matter how much they run;
  OpenRouter is the only metered leg, fenced to fallback + bulk and watched by `fallback-watch.sh`.
- *VPS CPU/RAM:* the arms are thin clients (compute is remote), so a CX23 (2 vCPU/4 GB) runs the
  dispatcher + several concurrent arms comfortably. The Box-1/Box-2 split (D-G) keeps a `claude -p`
  burst from OOM-ing the fleet workers; if parallel arms grow, bump Box 2 to a CX33 (8 GB) — cost is
  noise, resize freely.

Net: the VPS handles it, it runs consistently with your computer off, and "expensive traffic" is a
token question answered by the flat subs — not a bandwidth question.

## Naming — resolved 2026-08-05

Lua/Terra/Sol/Hermes began as a functional skin. The GPT 5.6 line then shipped models literally
named Sol/Terra/Luna, so `routing.yaml` binds each functional tier to the same-weight GPT model
(heartbeat→luna, brief/decision→terra, hard→sol; Hermes = the harness/front). Skin and models are
one thing per tier — see "The model roster" above. No rename needed.

## Build order — the dispatcher is the enabler, not a blocked step (2026-08-05)

The dispatcher does NOT wait on any particular workstream; those are workstreams it supervises, not
prerequisites. So: fleet first (active, near-done), the **dispatcher second** — the moment it exists,
every other workstream plugs into it and is run *through* Recursion instead of hand-opened. Building
it early changes how the later work gets worked; it is not step 2 of a queue.
