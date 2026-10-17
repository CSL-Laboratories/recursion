# Staged CI queue rollout (CSL-255)

Source landing does not activate merge queue enforcement. Keep direct `Devin Review`
and existing `linux` protection. Do not change repository settings in this task.

Corrected source order: settle replacement for #213, queue replacement for #215,
then etiquette replacement for #214. Preserve separately owned #211 -> #216 and
their reviewer/fixer roles, receipt trust, locks, retry caps and resolver policies.
Their source landing order and the existing cross-repository hold require owner
clearance. Do not replace those branches or import unrelated policy/planning work.

## Gates and activation holds

- The existing required name `linux` is now a small hosted aggregate. Paid work
  runs as `linux-tests` only after acceptable settle. The required aggregate
  always evaluates, fails on cancelled/failed/unexpectedly skipped settle or
  skipped/failed Linux tests, and forwards verified path outputs. No protection
  setting change is needed to keep failed settle from satisfying `linux`.
- `platform-gates` waits for settle, Linux, Windows and macOS. Queue and manual full
  runs and every ordinary PR candidate (including docs) require all three platforms. A Linux-only
  PR run can pass required `linux`, but its staged `platform-gates` fails until
  a verified exact-candidate READY admits complete coverage. Master push retains Linux/Windows fallback;
  nightly macOS remains. This is deliberate until separately approved activation.
- Outside ordinary PR candidate readiness, a docs-only no-op requires Linux success plus the exact `docs-only` classification
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
still execute CI. All label events now have separate concurrency groups and check
names and never admit paid CI. The exact-candidate READY caller reuses this CI DAG
in a separate per-PR candidate concurrency group. Neither lane cancels the other;
a new push does not cancel candidate work, and the final audit rejects stale results. GitHub concurrency queue order is not dispatch
order: even a delayed Linux-only run must fail the ordinary PR aggregate.
Manual dispatch remains independent. After a paid job starts, cancellation cannot
recover elapsed cost. Coverage and correctness
precede the $100/month target. No savings or invoice reduction is established here.

No queue canary, protection change, merge, rebase, force push, credential creation
or permission expansion is authorized by this document. Remove master fallback
only after separately approved queue enforcement and validation.

Sources: [required checks](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks),
[workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows),
[commit statuses API](https://docs.github.com/en/rest/commits/statuses).

## Exact-candidate readiness follow-on (B1/B2)

After bootstrap on the default branch, the existing locked gate invalidates only
its proven owned stale `ci:full` label before requesting a review. Human labels,
removals, ambiguous writes and fixer holds retain the receipt helper's policy.
An unchanged candidate reservation keeps its label; the label itself is never
review or CI evidence. No shared receipt-helper policy changes are included.

A candidate binds repository, PR, full head SHA, live base SHA, synthetic merge
SHA and tree SHA. The merge parents must be exactly base then head. The existing
PAT authors one immutable request per candidate, at most three per head per UTC
day. The authoritative latest review must come from the exact verified Codex bot
identity, follow that request, contain exactly two plain lines: `CODEX-CANDIDATE: <digest>` followed by
`CODEX-GATE: READY`. Prose, quotation, fences, hidden HTML, duplicate markers and
contradictory text anywhere in an affirmative body fail closed. A reviewer with
findings submits a non-READY review or inline comments. Submission timestamps order terminal reviews; creation IDs cannot prove attempt order.
Tied latest submissions and unbound/pending trusted evidence fail closed.
Old head-only READY, pending/dismissed reviews, wrong identities,
new negative attempts and stale base/tree evidence do not qualify.

The existing verdict writer authenticates the current receipt/hold state under
the shared PR lock and records an immutable Actions reservation before exporting
admission. That review event calls the existing provider-free CI workflow with
pinned merge/base/tree identities and **forces all three platforms, including docs**.
The matrix receives contents-read only and no PAT. Each platform job verifies the
pinned merge/tree checkout with shell builtins and git before installing the
interpreter; Windows and macOS cells then invoke `python`, never a runner-provided
`python3`. A read-only final audit checks
the current candidate, authoritative review, reservation, label generation and
receipt state after all platforms succeed. New findings requiring a journal write
fail back to the locked writer. `candidate-readiness` is staged and is not yet a
required protected check. Direct Devin enforcement remains unchanged.

Each candidate has one workflow attempt. Duplicate review events cannot reserve
another run. Candidate job conditions reject attempts greater than one before
paid runners start: GitHub partial reruns may reuse old successful admission job
outputs, so they cannot safely serve as recovery. A failed/cancelled candidate
requires owner-led integration to a new candidate and a fresh independent review;
manual full CI remains a diagnostic lane and cannot establish candidate readiness.
No automated mutation retry is performed after an uncertain request/reservation.
Investigate the existing receipt before any human recovery; do not delete history.

Submitted, edited and dismissed review events recheck authoritative evidence and
revoke only proven owned labels when it no longer qualifies.

This source does not provide continuous revocation or atomic merge-time enforcement.
A base advance alone does not guarantee a PR event. The owner must compare the
live candidate immediately before approval/landing, and use the gate's existing
`workflow_dispatch` with `pr_number` to request review after a base change. Dispatch
requests a review; it never directly admits candidate CI. A stale/unavailable
synthetic merge fails closed; retry the request after GitHub refreshes it. Results
can become stale after the final read. These limitations, protected authority,
bootstrap deployment and real review-event validation remain activation holds.
REST Actions `head_sha` is not interchangeable with native event `GITHUB_SHA`;
the latter identifies the review event's synthetic merge checkout. No arbitrary
PR status is written from a default-branch event.

Sources: [review event identity](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_review),
[reusable workflow context and reruns](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations).

Authenticated MEDIATE events route through the existing non-admitting mediation
path after checking the live head and authoritative latest review, before any
candidate lookup. A `CODEX-GATE:` line counts only as the final nonblank plain
line of the review body outside any code fence; indented, quoted or fenced copies
and trailing content leave no verdict, and the candidate path then treats the
review as non-READY. Superseded events use the ignored check lane and cannot replace
newer candidate readiness. A current MEDIATE revokes only proven owned readiness,
even when its legacy dispatch receipt already exists. Conflicts, stale synthetic merge refs and an
advanced base do not prevent mediation. The existing missing-PAT, fork/draft and
authenticated deduplication rules still apply; mediation never reserves a matrix.
Mediation rechecks head, review, human removal and external pause generation around
owned invalidation and immediately before the legacy receipt/dispatch writes. Only
the exact authenticated POST response IDs from this invocation are excluded from
the pause comparison; a newer own receipt cannot hide an intervening external pause.
If evidence changes after the legacy acted receipt was posted, dispatch stops and
the owner must inspect that receipt before recovery. This is bounded revalidation,
not an atomic transaction or permission to retry uncertain delivery.

Request/review timestamps have second precision. Equal-second request and review
observations cannot establish causal ordering and remain rejected, as do tied
latest review submissions. This is an explicit liveness limit: an owner must
obtain a fresh independent review in a later second for the same still-current
candidate, without deleting the request/claim history. No automatic request replay
or assumed ordering across unrelated comment/review IDs is used.

A workflow_dispatch full-platform diagnostic does not exercise the candidate-only
pinned-checkout and one-attempt guards because its candidate input is empty.
Passing product tests on all OSes is separate evidence from genuine review-event
admission/reusable-workflow qualification; the latter remains an activation hold.

## Cross-repository design notes (Cockpit168 findings, assessed 2026-10-17)

- **Queue starvation (Cockpit 4300898366): absent.** The gate holds no
  repository-wide concurrency group. Its workflow group is per PR and per review
  event; the `request` and `verdict` jobs wait only in the per-PR `claude-<pr>`
  writer queue. A long fixer run delays that one PR's request/verdict;
  `queue: max` bounds the backlog, not the wait time. This nested queue does not
  hold a repository-wide gate lock or displace other PRs or scheduled scans.
- **Self-qualifying candidate (Cockpit 4300898369): applicable, held.** The
  default-branch sparse checkout protects only the script bytes. On `pull_request`
  and `pull_request_review` events the gate workflow and the same-commit local
  `ci.yml` definitions come from the PR merge ref, so a same-repository writer PR
  controls the job conditions, checkout verification, matrix inputs, the final
  audit step and the `candidate-readiness` check name. It can request write
  `GITHUB_TOKEN` scopes and reference available secrets, subject to repository
  and environment controls. Ordinary fork PR restrictions do not protect this
  same-repository case. `candidate-readiness` therefore remains staged diagnostics, never a
  protected replacement authority; a trusted default-branch verifier would be a new
  contract and needs an owner decision.
- **Shared Actions identity (Cockpit 4300898370): partially applicable.**
  Request markers are bound to the PAT writer identity and the three-per-head
  daily budget counts only that writer, so `GITHUB_TOKEN` writers cannot forge
  or exhaust requests. Claim markers are authenticated only as `github-actions[bot]`,
  which every `issues`/`pull-requests` write workflow shares. A forged claim for the
  current candidate with a foreign run ID can suppress admission (ignored lane,
  no mutation), while duplicate claims fail closed. A same-run claim bound to
  another review now stops. Matching public run/review fields still do not prove
  gate origin; this fix does not authenticate claims or replace the independent
  review, label and final-audit checks. Gate-exclusive proof needs an owner
  decision. A new HMAC secret or dedicated app is outside this authorization;
  Actions run attestation alone also cannot make PR-controlled YAML trusted.
