# CSL-255 contract review (v2) — for Samir

**To:** Samir (design review)  
**From:** Lucas, via Cursor Cloud (attributed relay; not a developer-approved receipt)  
**Date:** 2026-10-25  
**Scope:** design and proof preparation only. No implementation, no permission changes, no hosted or paid runs, no PR, no workflow edits.  
**Candidate (preserve, unpublished):** `48763f1fc7a21f21e1a79c3751b7c70b2164fef7`  
**Base:** `e10fbe80b9695e8f44fce3da129138ca825a3345`

Codex’s adversarial review of v1 was **CHANGES REQUIRED**. This v2 chooses the **contract first**, then names mechanisms. It does not ask for implementation or hosted-test approval.

Platform sources used below:

- [Control workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
- [Cancel a workflow run](https://docs.github.com/rest/actions/workflow-runs#cancel-a-workflow-run)

---

## Contract options (choose one)

GitHub concurrency evaluates a group name on the **incoming** run only. It does not compare that run to a live PR head. The cancel API is a separate, non-atomic `POST` (`202` / `409`). Those are platform facts. They constrain native grouping; they do not by themselves prove that every broader admission design is impossible.

### (i) Current-head protection is strict; obsolete work is accepted

This is **option A**.

- In-progress work on the live SHA must not be cancelled by stale events, twins, or reruns of another SHA.
- Obsolete commits may finish settle and start paid tests after a newer commit arrives.
- Smallest mitigation: keep unpublished candidate `48763f1` (SHA-isolated PR groups, `cancel-in-progress` false for `pull_request`, no job-level PR group) and rewrite `docs/ci-event-contract.md` so superseded-head cancellation is no longer a requirement.
- No new permissions. No privileged canceller.

### (ii) Best-effort cleanup of obsolete work, with stated residual risk

This is **option C, reframed as cleanup only**.

- Keep SHA isolation so native grouping does not cancel current-head work in another SHA’s group.
- Add a later, trusted cleanup path that *tries* to stop non-live SHA runs.
- This is **not** “both guarantees satisfied.”
- Residual risk that must be accepted in the contract:
  - **Current-head is not strictly protected:** read live = B → restore A → cancel A still cancels the now-current commit. Re-read-before-cancel shrinks the window; it does not close it. The cancel API is not compare-and-swap on live head.
  - **Obsolete starts are not zero:** delayed events and cancel latency can let old work begin (and even finish settle) before cleanup takes effect.
- Hosted tests can verify triggers and permissions. They **cannot** disprove that race.

### (iii) Both guarantees remain strict

Do **not** add `actions: write` in order to “make C strict.” Native grouping has no live-head comparison, and cancel-after-read is not atomic, so tightening C does not yield strict R1 and strict R2.

This contract needs a **redesigned admission/authority model** before any write privilege: a single trusted step that decides which head is authoritative **before** any paid job starts, and/or required checks that bind to a recorded head. Sketches are in “If both guarantees stay strict.” They still have residual gaps; those gaps are listed rather than claimed impossible.

---

## Corrected guarantee matrix

| Contract | Strict current-head protection (no cancel of live-SHA in-progress work) | Zero obsolete paid starts | What it actually is |
|---|---|---|---|
| **(i) / A** — SHA isolation, no canceller | **Yes**, within native grouping: other SHAs cannot join the live SHA’s group, so they cannot cancel it. Same-head twins queue; they do not cancel in-progress. | **No.** Obsolete SHA runs keep going. | Current-head mitigation. Obsolete work is accepted. |
| **(ii) / C** — SHA isolation plus trusted cancel cleanup | **No.** Read B → restore A → cancel A still cancels the now-current commit. Cleanup that cancels “SHA ≠ last-read live head” is not strict R1. | **No.** Delayed events and cancel latency can start old `settle` / `linux-tests` before `POST .../cancel` lands (`202` is not instantaneous; `409` means the run could not be cancelled). | Best-effort cleanup with acknowledged residual risk. |
| **Master / B** — PR-wide group, `cancel-in-progress: true` | **No.** Same-head twins and ready-for-review cancel in-progress current-head runs; cancelled `linux` (`if: always()`) fail-closes and can overlay a later success on that SHA. | **No.** B does **not** guarantee zero obsolete starts. A delayed event can be admitted and start work; cancellation takes effect only after GitHub processes it. Jobs already dispatching can begin before the run is cancelled. | Today’s superseded-cancel behaviour. Not a strict R1 or strict-zero-start contract. |
| **(iii)** — both strict | **Not offered by A, B, or C.** Needs a new admission/authority model (below). | **Not offered by A, B, or C.** | Open design, not a drop-in workflow flag. |

Citations: concurrency groups, one running + one pending, pending replacement even when `cancel-in-progress` is false, and `cancel-in-progress` applying only to the running occupant — [concurrency docs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency). Cancel endpoint semantics (`202` / `409`), non-transactional with any live-head read — [cancel a workflow run](https://docs.github.com/rest/actions/workflow-runs#cancel-a-workflow-run).

**Hosted tests:** useful for “did `workflow_run` fire,” “did `contents: read` GET this ref,” “did cancel return 202.” They cannot prove away the B→A restore race or cancel latency. Do not treat a green hosted scenario as evidence that strict R1 and strict R2 both hold.

---

## If both guarantees stay strict

Two design-level sketches. Neither is admitted. Neither should be implemented until this contract is chosen. Neither claims atomicity the cancel API and concurrency docs do not provide.

### Sketch 1 — Trusted admission before any paid job

**Idea:** Unprivileged `pull_request` CI may run cheap preflight (and optionally settle). It must not start `linux-tests` / windows / macos. A default-branch workflow (for example `workflow_run` of that unprivileged workflow) is the only authority that may start paid work, and only for the SHA it has just read as live.

**Trust boundary:** Orchestration YAML, tokens, and the admit decision live on the default branch. Test *code* is still the PR commit (that is the point of CI). The PR merge ref must not receive `actions: write`. Do not put admission inside `ci.yml` on `pull_request` (the workflow file is the merge commit). Prefer `workflow_run` over `pull_request_target`.

**Minimum permissions (trusted workflow only):** `contents: read` for live same-repo ref / commit; `actions: write` only if that workflow dispatches or cancels the paid run. Do not request `pull-requests: read` unless a hosted 403 on GET `/pulls/{n}` with `contents: read` is recorded.

**Still does not guarantee:** a live-head read followed by starting a runner is not atomic. Head can move in that interval, so obsolete paid work can still begin. If admission then cancels the just-started run, the B→A restore race returns. Docs do not provide compare-and-start.

### Sketch 2 — Required check bound to a recorded head

**Idea:** A trusted job records `(PR, SHA, admission id)` and posts the merge-relevant check or commit status **only** for that recorded head (for example `linux-live`). Branch protection requires that name. Unprivileged `linux` on other SHAs is not what merge consumes.

**Trust boundary:** The poster runs default-branch code. It must not execute PR-controlled workflow YAML. Recorded admissions are data, not a GitHub concurrency key.

**Minimum permissions (trusted workflow only):** `contents: read`; `checks: write` and/or `statuses: write`. Still no `actions: write` unless this sketch also starts or cancels runs.

**Still does not guarantee:** obsolete paid jobs can still start; this sketch changes which check merge trusts, not which runners GitHub allocates. Two admitted twins on the same SHA can still last-completer-overlay. Recording the head is a snapshot, not a lock.

A generation counter (cancel admission *id*, not SHA) avoids cancelling a *new* A run after rollback, but it still cancels the original A run if that run is the one that became current again before a new event is admitted. That is the same R1 hole as C, moved into the admission ledger.

---

## Cost/capacity impact

Placeholder: a separate analysis is gathering real numbers. Those figures will be merged here before any privileged-cancellation admission. Until then, do not assume obsolete SHA isolation is cheap enough to ignore, and do not assume a canceller is justified on cost.

---

## Unpublished candidate

`48763f1` remains on `csl-255-current-head-cancellation` only. It is the A mechanism (and the grouping half of C). It is **not** the completed CSL-255 fix while superseded-head cancellation is still a requirement. This docs branch does not contain that candidate.

---

## Questions for Samir

Please answer in Linear from your own account. No implementation and no hosted-test approval until the contract is chosen.

1. **Which contract?** (i) strict current-head, accept obsolete work (A); (ii) best-effort cleanup with the residual races above (C, cleanup only); or (iii) both guarantees strict, which means redesign admission/authority before write privileges.
2. If **(i)**: is the next admitted change “publish `48763f1` + rewrite `docs/ci-event-contract.md`,” and should superseded-head cancellation be dropped from CSL-255 acceptance?
3. If **(ii)**: do you accept that C does **not** preserve strict current-head protection (B→A restore race) and does **not** guarantee zero obsolete starts (delay + cancel latency)? If yes, cleanup still waits on cost/capacity numbers and a separate file/permission admission; it is not approved here.
4. If **(iii)**: which sketch (trusted admission before paid, recorded-head required check, both, or a different authority model) should be designed next? Confirm that `actions: write` stays out until that model is accepted.
5. Should we wait for the cost/capacity numbers before considering any privileged cancellation system?
6. Confirm hosted tests, if later admitted, are for triggers and permissions only and cannot disprove the known race.
7. Are `ci-full-label.yml` windows/macos tails in the same contract, or out of scope?
8. You are the requested human reviewer for this design. Please say whether you accept that review role for v2.
