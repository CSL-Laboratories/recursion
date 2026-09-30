---
title: DS-09 — Interactive terminal and communication views
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-09 — Interactive terminal and communication views

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Decide the persistent process, client protocol and rendering architecture that supports several detachable Head/ARM/worker views with clean termination and negligible passive overhead, plus causal communication visualization. Agenda lane R11 blocks the terminal runtime contract until native Windows and Linux prove attach/detach, flow control, cursor replay and full descendant-tree termination.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. The product ruling that terminals detach rather than end work is fixed; this lane compares mechanisms and may not reopen it.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/RESEARCH-AGENDA.md`
- `docs/v3/TERMINAL-RUNTIME.md`
- `docs/v3/CLI.md`
- `docs/v3/INTERACTION-SURFACES.md`
- `docs/v3/PERFORMANCE-PLAN.md`
- `docs/v3/SECURITY.md`

## Research questions

1. Which creation-time ownership primitive gives true descendant-tree termination on Windows (ConPTY plus a Job Object via `PROC_THREAD_ATTRIBUTE_JOB_LIST`) and on Linux (PTY plus session/process groups, with cgroup v2 or `clone3(CLONE_INTO_CGROUP)` for stronger scope), and where do breakaway, migration or alternate creation escape it?
2. How do attach, detach and reconnect preserve cursors and actor identity without relaunching a process or an effect?
3. What bounded flow-control and credit design guarantees a slow or absent client cannot block the canonical writer or drop durable events, while presentation frames may coalesce?
4. How do IPC authentication, protocol versioning and capability negotiation prevent a local terminal from gaining more authority than the active operator and GoalSpec?
5. Which TUI diff-rendering approach minimizes flicker, preserves typed input during background output, and handles Unicode width, resize and high-output bursts?
6. How are Git Bash/MSYS2 PTY translation and Neovim embedded-terminal behavior distinguished from native Windows and Linux stacks, and what tests graduate them?
7. Which multi-session failure modes (daemon restart, provider or node loss, orphaned grandchildren, PAUSE/kill races) must be reproduced, and what are their acceptance stop conditions?
8. How do service supervision options on each OS interact with graceful, force and emergency stop without silently restarting killed work?
9. For communication visualization, how should a stable actor/work graph and a selected causal event or message stream coordinate without merging private contexts or relying on color alone?
10. What plain-text and accessibility fallback proves the interface remains usable for screen readers and unsupported terminals?
11. How are stream cursors, replay floors, snapshot installation and RESET-on-gap specified so reconnect repairs a projection without relaunching effects?
12. What is the minimum PERF-010 matrix (0, 1, 5, 10 and 20 passive views under idle and high-output loads), and which measurements decide acceptance?
13. Which candidate implementation choices (renderer, multiplexer, IPC encoding) must remain unselected until measured?
14. What negative trace proves a design fails: terminal close kills work, a passive view launches execution, a slow client blocks canonical state, or the daemon cannot terminate its full descendant tree?

## Deliverable

A source-pinned report with: a claim table (claim, source, version/date, classification, confidence, expiry trigger); a process, IPC, flow-control and compatibility matrix; a preregistered cross-platform test plan including the PERF-010 view matrix; communication-view wireframes; a recommendation tied to a named competing option and decisive falsifier; and open Owner questions. No implementation is selected as a product default.

## Non-goals

- No runtime or terminal implementation.
- No selection of Zellij, tmux, Ratatui, crossterm or xterm.js as a product default.
- No OS terminal application made mandatory.
- No reopening of the accepted detachable-terminal product ruling.
- No claim of full descendant-tree termination before measured across the OS matrix.
- No graphical frontend work.
- No provider, model or route calls.
- No multiplexer or shell session established against any work.

## Completion gate

- Primary platform sources pinned by version/date (Microsoft pseudoconsole and Job Objects, Linux cgroup v2, POSIX.1-2024 terminal and kill, SSH RFC 4254).
- Each rejection criterion has a specified negative trace.
- The 0/1/5/10/20 view matrix and its stop conditions are preregistered.
- Native Windows and Linux are mandatory, with Git Bash/MSYS2 and Neovim as distinct high-priority cases.
- Accessibility and plain-text fallback are covered.
- Every recommendation names a competing option and a falsifier.
- Protected paths are untouched; privacy and secret scan are clean.
