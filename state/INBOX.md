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

## [esc-0002] 2026-10-26 — WP16 spine memo (csl-research#50) not readable; spine items beyond the stated minimum not built

**Status:** open
**Workstream:** CSL-495 / CSL-498 / CSL-503 (WP16), recursion stack off R#302
**Blocked on:** Dan or Lucas pasting or exporting the Recursion spine items from `submissions/20261026-csl522-wp11-nov11-spine-memo-01/result.md` (csl-research PR #50, branch `cursor/csl-522-wp7-wp11-drafts-8e75`), or granting this run read access to csl-research. The run's GitHub token returns 404 for that repository (`gh api repos/CSL-Laboratories/csl-research`, 2026-10-26).
**What I did NOT do:** Did not guess spine items. Built only what the task and Step 4 section 6 WP16 state: a recorded fake-driver run through an authenticated Control API on Linux for the Codex-style and Claude-Code-style paths, plus a frozen-SHA rerun script. No Control MCP, pairing, relay, CLI/TUI or GUI work.
**Options / recommendation:** (1) Dan confirms the built slice is the memo's Control API item and names the remaining items as follow-up PRs on the same stack (recommended). (2) The memo names different Control API semantics, and the slice is reshaped before review.

