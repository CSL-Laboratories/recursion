# Staged CI queue rollout (CSL-255)

Source landing does not activate merge queue enforcement. Keep direct `Devin Review`
and existing `linux` protection. Do not change repository settings in this task.

Corrected source order: settle replacement for #213, queue replacement for #215,
then etiquette replacement for #214. Preserve separately owned #211 -> #216 and
their reviewer/fixer roles, receipt trust, locks, retry caps and resolver policies.
Their source landing order and the existing cross-repository hold require owner
clearance. Do not replace those branches or import unrelated policy/planning work.

## Gates and activation holds

- `platform-gates` waits for settle, Linux, Windows and macOS. Queue and manual full
  runs require all three platforms. Ordinary PRs require Linux and any selected
  extra OS jobs. Master push retains Linux/Windows fallback; nightly macOS remains.
- A docs-only no-op requires Linux success plus the exact `docs-only` classification
  from a nonempty verified diff. Linux still runs public-scope and whitespace checks.
  Missing outputs, pending/failing/cancelled jobs and unexplained skips fail closed.
- PR diff selection uses the checked-out synthetic merge SHA (`github.sha`), not
  the unmerged PR head. Queue diffs use `merge_group.base_sha..head_sha`. Explicit
  malformed or mismatched head identities fail before classifying the tree.
- Before enabling queue, the owner must require `platform-gates` on PR and queue
  runs and prove direct Devin coverage on the **merge-group SHA**, or separately
  approve a protected replacement authority. `review-gate` is diagnostics only:
  PR-controlled YAML can change its implementation even when its script is loaded
  from the base. It must not replace direct Devin enforcement.
- `review-gate` authenticates commit statuses from bot ID 158243242, login
  `devin-ai-integration[bot]`, type Bot. This provider identity was observed on
  Recursion #211 head a59e6c91575175c4c8e643eeb126545bec30f2cd on 2026-10-16.
  A same-name check run is not a supported authority. It checks the response SHA,
  complete bounded pagination, latest numeric status ID, and the live PR head
  before/after evidence. A pending retry never falls back to older success.
  API failures, inconsistent snapshots and changed provider identity fail closed.
  A foreign same-name status blocks diagnostics on that SHA rather than being
  ignored; investigate it with the owner. Transient errors require manual rerun.
- Queue diagnostics require a genuine status on the queue SHA; they never pass
  merely because the event is `merge_group`. Such provider coverage is unproven.
  The initial base may lack this new script, so diagnostics fail during bootstrap.

## Bounded recovery

The diagnostic polls for up to 180 seconds (15-second HTTP timeout per read;
8-minute workflow timeout is the hard wall). Drafts and stale PR heads fail.
This is intentional diagnostics behavior, not an automatic fixer request.
No `status` event job is used: its native check would report on default-branch
`GITHUB_SHA`, not the late PR SHA. No status/check write grant is added.

If Devin finishes after timeout, a repository writer verifies the current PR head
equals the original run's event head, then reruns that original `pull_request`
workflow with `gh run rerun RUN_ID --repo CSL-Laboratories/recursion --failed`.
If the head advanced, use its new PR run. A manual `workflow_dispatch` diagnostic
is not a substitute for the PR-bound check. There is no universal automatic
late recovery guarantee; evidence can change after any snapshot.

## Cancellation and cost boundaries

Ordinary PR runs already share concurrency across heads. A non-draft synchronize
waits 120 seconds on hosted Ubuntu; cancelled/failed/unexpectedly skipped settle
cannot admit paid Linux or dependent OS jobs. Opened/ready/manual/queue runs
deliberately skip settle and can run their selected jobs. Draft event runs can
still execute CI. A push does not cancel independent label/manual lanes; after a
paid job starts, cancellation cannot recover elapsed cost. Coverage and correctness
precede the $100/month target. No savings or invoice reduction is established here.

No queue canary, protection change, merge, rebase, force push, credential creation
or permission expansion is authorized by this document. Remove master fallback
only after separately approved queue enforcement and validation.

Sources: [required checks](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks),
[workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows),
[commit statuses API](https://docs.github.com/en/rest/commits/statuses).
