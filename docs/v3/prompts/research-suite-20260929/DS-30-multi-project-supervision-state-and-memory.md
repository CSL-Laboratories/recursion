---
title: DS-30 — Multi-project supervision state and durable memory
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-30 — Multi-project supervision state and durable memory

This is an inert Deep Research handoff. It launches nothing, authorizes no provider call, write, spend or configuration change, and is inert until Lucas explicitly invokes this file in a new session. Run it as a public/synthetic research lane only; it covers supervision state and durable memory, not market or alpha research.

## Purpose

Decide the file and directory convention by which one operator plus unattended agents keep durable, diffable state for 4+ concurrent projects, so work resumes across context loss and blocked, stale and waiting-for-decision conditions are visible rather than inferred. This is blocking because Recursion already committed to git-as-transport and a `state/` tree (`CONTROL_PLANE.md`, D-011, D-014); a convention that becomes a second source of truth silently stales and reads exactly like working state. The research must reconcile with, not replace, that layout and the unattended-loop requirements already established for it.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed reconciliation, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. Do not reopen accepted Owner decisions or ANCHOR scope, and do not select a language, store or framework. Git-as-transport and the live-clone model are fixed context to reconcile against, not options to reopen.

## Inputs to read

- `CONTROL_PLANE.md` (git-as-transport, PAUSE, invariants)
- `ANCHOR.md` (capacity and kill criteria)
- `AGENTS.md` (operating rules)
- `docs/v3/START-HERE.md`
- `docs/v3/STATE.md`
- `docs/v3/CONTEXT.md`
- `docs/v3/TERMINAL-RUNTIME.md`
- `docs/v3/CLI.md`
- `docs/v3/EXECUTIVES.md`
- `state/portfolio.tsv`
- `state/DECISIONS.md`
- `state/INBOX.md`
- `state/BOARD.md`
- `state/goals/goal-recursion-v2-execution-fabric.md`
- `state/tasks/v3-orientation-20260913.md`

## Research questions

### File-convention state and context-loss survival

1. Which file conventions (CLAUDE.md, AGENTS.md, per-repo status files, JSON/YAML state) have documented survival of context loss, and what exactly survives a fresh session versus only a compacted one?
2. What minimum fields must a state record carry so a fresh agent can reconstruct current task, gate, authority, next action and unresolved evidence without a transcript?
3. Where have file-convention approaches silently staled in real repositories, and what signal distinguished stale from quiet-working state at the moment it was missed?

### Structured versus freeform state and diffability

4. In documented solo or small-team setups, does schema-checked structured state reduce resume error versus freeform prose, and when does structure itself become maintenance debt?
5. Which state fields can be derived from git (fetch result, HEAD, dirty tree, branch) so they are never hand-copied, and which genuinely cannot live in git?
6. What diffable representation of task status lets an agent mechanically detect change between two cycles without re-reading every document?

### Representing blocked, stale, and waiting-for-decision

7. How are blocked, stale and waiting-for-decision distinguished from each other so an unattended loop neither re-asks an answered decision nor silently proceeds past one?
8. What staleness signals (age, last-fetch commit, heartbeat age, missing artifact checksum) reliably classify a project, and where does each produce false positives?
9. How is "waiting for a decision" correlated to the exact Owner question and next action so the same escalation is not appended repeatedly, as happened before D-010?

### Retrieval and long-horizon agent memory

10. Which long-horizon memory architectures are documented (retrieval-augmented, summary hierarchies, vector or graph stores, file-based memory) and what measured failure modes do they report: drift, contradicted facts, lost provenance, unbounded growth?
11. In published memory evaluations, what do agents get wrong about knowledge updates, temporal recall and abstention, and does any of that transfer to repository supervision?
12. What retrieval and indexing strategy keeps 4+ project histories searchable without cross-project leakage or context blowup, and how is a wrong retrieval detected rather than trusted?

### SRE monitoring for a one-person operation

13. Which SRE patterns (heartbeat, staleness alert, error budget, runbook, blameless postmortem, golden signals) translate to a one-person multi-project operation, and which assume an on-call rotation that does not exist?
14. How is silent degradation — a loop that runs but makes no verified progress — detected without continuous model inference, and what false-negative remains?

### Unattended loop architecture and guardrails

15. Compare scheduling (cron/batch), event-driven, and persistent daemon architectures on state persistence, handoff, crash recovery and cost for unattended overnight operation.
16. What guardrails appear in working unattended-agent setups against runaway loops, retry storms, context bloat and duplicate side effects?
17. How should supervisor/worker escalation be structured when the supervisor itself may be the degraded component?
18. What do published operator post-mortems of unattended agents report as root causes, and which are preventable by state convention versus only by runtime containment?

### Recommended convention for 4+ concurrent projects

19. Given `state/`, `portfolio.tsv` and git-as-transport, what convention is additive rather than a parallel source of truth, and what example schemas serve a single operator supervising 4+ concurrent projects?
20. What concrete observable falsifies the recommended convention — for example a stale blocker rendered as current, an agent re-asking an answered decision, or state that cannot be diffed across two commits?

## Deliverable

- A source-pinned claim table (claim, source, version/date, classification as fact/inference/hypothesis/proposal, confidence, expiry or falsifier trigger).
- Comparison matrices for file-convention versus structured state, and for cron versus event-driven versus daemon, with every unmeasured cell marked unknown.
- A recommended file and directory convention with example schemas for 4+ concurrent projects, shown as a reconciliation that reuses `state/` and git-derived status rather than replacing them.
- One recommendation tied to a named competing option and a decisive falsifier.
- Open Owner questions listed separately.

## Non-goals

- No market, investment or alpha research.
- No runtime, daemon, scheduler, database or storage implementation.
- No language, store or framework selection.
- No reopening of git-as-transport (D-011), PAUSE behavior (D-012, D-014) or ANCHOR scope.
- No private project content, transcripts, local paths or credentials in any public packet.
- No provider, model or route calls, and no benchmark executed.
- No invented schema, threshold or numeric budget presented as a requirement.
- No replacement of `state/DECISIONS.md` or the morning-brief discipline.

## Completion gate

- Every factual claim is pinned to a primary source with version or retrieval date, or marked unverified.
- The recommended convention is explicitly additive to `state/` and git-derived state, and names what it does not duplicate.
- Blocked, stale and waiting-for-decision each have a distinct, diffable representation.
- Unattended-loop guardrails cover runaway loops, retry storms, context bloat and duplicate effects, each with a falsifier.
- Silent-degradation detection is proposed with its known false-negative stated.
- Every unmeasured matrix cell is marked unknown rather than assumed.
- Every recommendation names a competing option and a decisive falsifier.
- Open Owner questions are listed and no decision is promoted into an Owner decision.
- No protected path is edited and no runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge effect occurs.
