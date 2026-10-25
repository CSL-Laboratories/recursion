# Inbox — escalations and blockers

Record a blocker here when a task is ambiguous, needs a decision not recorded in
`state/DECISIONS.md`, conflicts with the research, or makes a claim you cannot verify.
Do not guess — write the blocker and stop.

## Template

```
## [esc-0001] YYYY-MM-DD — <one-line question>
**Status:** open
**Workstream:**
**Blocked on:**
**What I did NOT do:**
**Options / recommendation:**
```

## [esc-0001] 2026-10-24 — Publisher thin-call trust root (default branch vs live PR base)

**Status:** resolved 2026-10-24 — Samir (Linear comment `72d408ec`): B with reachability, not refuse-on-move.
**Workstream:** CSL-414 / recursion#285 (`lucassalvador987/csl-414-deferred-commit`)
**Blocked on:** —
**What I did NOT do:** Did not use `github.workflow_sha` as the sole source. Did not refuse non-default bases.
**Options / recommendation:** Implemented B: prepare records the default-branch SHA; publish loads that SHA if compare vs the current tip is `identical` or `behind`. Refuse with terminal audit only if the SHA is not on the default branch or the blob is missing.
