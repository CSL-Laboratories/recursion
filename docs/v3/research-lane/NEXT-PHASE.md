---
title: Next phase — research-to-experiment handoff (fresh context)
status: frozen-historical
parent-goal: goal-recursion-v3-harness-orchestrator
last-updated: 2026-10-25
---
# Next phase — fresh-context handoff

**Frozen historical runbook** (intakes through 2026-10-02). New research output goes to
[CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research) as
`submissions/<record_id>/`, through a PR in that repo, with a digest on the Linear
issue. Never commit research content to this repo; this repo only links to the
vault record (repo, 40-hex commit, path, claim, limitations). New non-public docs
(research, specs, decisions, evidence and receipts, gate artifacts, and runbooks
that aren't public) go to that vault through a PR there, except operational
contracts and runbooks that this repository's code, a gate or CI reads: those
stay here. A file stays in this repository if code, a gate or CI here reads
it; everything else non-public goes to the vault. Existing files stay;
CSL-536 will move them. The existing decision logs (`state/DECISIONS.md` and `docs/v3/DECISION-REGISTER.md`) and their founder-approved authoring process are unchanged. The only other new docs allowed here are public knowledge-base,
README, setup and implementation instructions for outside readers, and agent
instruction files. If
csl-research is not accessible, stop and post a blocker on the Linear issue; do not
fall back to this repo. `docs/v3/research-lane/`, `docs/v3/foundation-research/` and
`docs/v3/prompts/` are legacy and frozen; `experiments/v3/` holds only runnable
product checks.

Read the historical packet first on a new context that is continuing that wave, then
`docs/v3/START-HERE.md`, `docs/v3/research-lane/SYNTHESIS.md`,
`docs/v3/research-lane/GAPS.md`, and the relevant FU spec under
`docs/v3/research-lane/followups-20261002/`. This file is a frozen runbook, not authority.

## Where we are

- Research is complete at the **specification** level: 28 lane reports + 5 follow-up specs
  (FU-01…FU-05) + a read-only architecture audit. Nothing was executed.
- A static audit (pin `3a542c6bbd88351e6f6c5eceffd317bc2450fcad`) concludes the tree is a
  **contract-and-experiment foundation, not yet a usable v3 harness**.
- No language, store, renderer, plugin runtime or protocol revision is selected.
- Authority: Hermes-led repository control and v1/v2 are retired (Lucas, 2026-10-02); v3 is
  standalone and also wraps upstream harnesses. Some entry points still contain stale
  Hermes/v2 migration language and need a control-plane authoring reconciliation.

## Owner decisions (updated 2026-10-02)

Settled and recorded in `state/DECISIONS.md` D-018…D-039:

- Thesis, release model, availability/failover, delegation-over-intermediation, pause/stop,
  first qualified profile, frontier claim, research plugin, license, counsel packet.
- **G-06 closed** (D-020); **G-07 closed** (D-022). The 2026-10-02 research recommendation of a
  smaller initial release was **rejected** (D-019); build comprehensively, install opt-in.

Still required:

- **G-11** closed by `D-028` (Windows 11 25H2/26200 min; Linux 6.12 LTS/6.6 min with cgroup v2;
  WSL2 dev/test only; local NTFS/ext4 only; power loss = abrupt VM reset, replay-identical).
- **G-32** licensing/counsel (packet R-C commissioned; no spend).
- **Selections** (after the experiments): language, store, renderer, plugin runtime.
- Always human-gated: spending/quota, credentials, live provider/host actions, deployment,
  publication, merges, autonomy flags.

## Research commissioned 2026-10-02

The scope-reframe handoff is intaken at `docs/v3/research-lane/scope-reframe-20261002/`. Four
autonomous research packets were handed to the research agent
(`docs/v3/prompts/dot-research-20261002/`); their **return** (46 questions) is intaken at
`docs/v3/research-lane/dot-ra-rd-20261002/` with a synthesis at `.../synthesis/SYNTHESIS.md`.
Nothing was executed; G-32 stays open; the next step is reconciliation + Owner acceptance of any
candidate contract (see the synthesis's local handoff).

- **Reconciliation draft:** `dot-ra-rd-20261002/synthesis/RECONCILIATION.md` — R-A/R-B shared
  semantics crosswalked against D-018…D-033 (draft contract candidate, not adopted).
- **Next research wave issued:** `docs/v3/prompts/dot-research-20261002b/` — **21 frontier
  handoffs R-E…R-Y** (trusted launch boundary, headless delegation/fan-out, plugin import,
  counsel operating-mode, Grok Build source study, multi-agent terminal UX, terminal multiplexer/
  TUI, interface platform, Claude Code terminal modding, graphical client, backend performance,
  prompt-injection defense, durable memory, long-horizon autonomy, adaptive optimization,
  multimodal I/O, sandbox/supply chain, observability, evaluation science, node fabric, workspace
  graph); zip handed to the Dot. Coverage maps: `ARCHITECTURE-COVERAGE.md`, `HARNESS-COVERAGE.md`.
- **Anti-drift map (frozen):** `docs/v3/research-lane/RESEARCH-INDEX.md` — historical waves
  through 2026-10-02. New artifacts land in csl-research `submissions/<record_id>/`.
- **Capability-composition return (packet d, revision 2):**
  `docs/v3/research-lane/capability-composition-20261002d/` (R-PC composition compiler, R-UI
  `recursion.ui-contract@candidate-2`, R-MOB mobile/remote control; +`check_intake.py`). Research
  evidence only; all fixtures unexecuted; no technology selected.
- **iOS/Android design + language expansion (O-04):** `docs/v3/research-lane/ios-android-20261002/`
  (advisory iOS Swift/SwiftUI and Android Kotlin/Compose stacks; shared `MP01…MP11` fixtures;
  +`check_intake.py`). Research evidence only; prototypes/tests unexecuted; no stack selected.
- **Packet-E return + PR #160 review:** `docs/v3/research-lane/core-language-control-surfaces-20261002/`
  (R-LANG core-language/reconstruction; R-CS control-surface study; Astra static review of the R-PC
  suite + verifier reconciliation). Research evidence only; no technology selected; the R-PC suite is
  a scaffold (rerun ACCEPT, full-spec fidelity ISSUES/NEEDS-EVIDENCE).
- **Packet-F R-AUDIT return + PR #165 review:** `docs/v3/research-lane/packet-f-raudit-20261002/`
  (A1–A10 corrections + M1 executable corpus + R-LANG C1–C6 + architecture revision guide; PR #165
  executed REJECT). Next: revise `docs/v3/architecture/LANGUAGE-AGNOSTIC-ARCHITECTURE.md` (#161)
  against it, then re-review.
- **R-PLAN + R-OPS prompts (packet 2026-10-02g):** `docs/v3/prompts/dot-research-20261002g/`
  (planning/executives facet; operations/lifecycle facet; +`check_research_stack.py`). Issued;
  two handoffs. M1A discrepancies remain open research questions; this packet does not resolve
  them.

## Next steps in order

1. Owner: approve authority reconciliation; answer G-06 (first), then G-07/G-11.
2. Run prerequisite experiments locally (this phase):
   - **FU-03** formal model + 36 mutants + language-neutral byte vectors — spec:
     `docs/v3/research-lane/followups-20261002/raw/ds-FU-03-formal-model-vectors.md`.
   - **FU-04** OS platform conformance fixtures — spec:
     `docs/v3/research-lane/followups-20261002/raw/ds-FU-04-os-conformance.md`; proposed
     machine specs under `docs/v3/research-lane/followups-20261002/machine/`.
3. Then dependent experiments: **FU-01** (language/runtime), **FU-02** (store crash/replay),
   **FU-05** (broker protocol + native chaos), using the accepted FU-03/FU-04 outputs.
4. Independent acceptance of the receipts (separate session/verifier).
5. Then selections and construction (audit's proposed bounded packets).
6. Incorporate the capability-composition candidates (packet d): Owner review of the R-PC/R-UI/R-MOB
   contract choices and the unresolved offline-retention policy; then independently checked,
   provider-free conformance fixtures under separately admitted tasks (R-PC F01–F18, R-UI
   UI-01…UI-20, R-MOB MOB-F01…F24). No technology selection is implied.

## Local execution discipline

- Each experiment needs a runnable, provider-free gate and an independent verifier that reruns
  the exact candidate; an implementer's own PASS is never acceptance.
- Destructive fault injection (disk exhaustion, forced kill, interruption) only on disposable,
  explicitly approved test systems with recovery and stop conditions.
- A process kill is not a power-loss pass; a database writer lock is not an authority lease;
  a transport ACK is not a settled effect.
- Record state distinctly: research finished / spec proposed / spec accepted / not run /
  inconclusive / floor failed / qualified / Owner selected. Never map `unknown` to success.
- The Codex Dot is research-only: it cannot run these experiments.

## Machine readiness

Ready (installed 2026-10-03): Python 3.12.10 (stdlib `sqlite3`, `hashlib`, `struct`, `zlib`),
git 2.48.1, Node 24, WSL `Ubuntu-24.04`, **Java 21.0.12.1 (Temurin)**,
**Rust 1.99.0 / Cargo 1.99.0**, **Go 1.27.0**.

TLA+ tools: `tools/tlaplus/tla2tools.jar`, official release **v1.7.4** (TLA+ 1.7.4 / TLC 2.19,
rev `5a47802`), fetched from the official GitHub release on 2026-10-03 — SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`, 2,274,532 bytes. The binary
is gitignored (`tools/tlaplus/`); pin this hash into the FU-03 run identity and re-hash if the
local bytes change.

- Run the readiness gate: `python -X utf8 -B tools/check_next_phase.py`
- Reinstall/refresh the toolchain (winget): `powershell -ExecutionPolicy Bypass -File tools/setup-next-phase.ps1`

## Resume checklist for a fresh context

1. `git fetch origin && git checkout <branch> && git pull`
2. Read: `docs/v3/START-HERE.md`, this file, `SYNTHESIS.md`, `GAPS.md`,
   `RESEARCH-INDEX.md`, and issued packet-g prompts at
   `docs/v3/prompts/dot-research-20261002g/`.
3. Run `python -X utf8 -B tools/check_next_phase.py` and resolve any MISSING line.
4. Ask Lucas for G-06 (and the authority-reconciliation approval) before any construction.
5. Begin FU-03/FU-04 in separate worktrees with their own gates.

## Non-goals

- No technology selection, no construction, no spend, no live provider/host action, no
  deployment, no publication, no merge, no edits to protected paths.
