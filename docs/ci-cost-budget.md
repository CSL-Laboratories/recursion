# CI runner budget and accounting (CSL-255)

Lucas approved this shared planning envelope on 2026-10-17: **$100/month,
with $60–75 planned core compute and $25–40 headroom**. This is a
planning target, not an enforced organization cap or permission for paid overage.
Account usage, invoice totals and credit allocation have not been verified.
[Implementation/approval attribution](https://linear.app/csl-laboratories/issue/CSL-255#comment-874d81e7-42fd-4658-9c82-76bc43c76c25).

## Runner change, 2026-10-21 (CSL-408)

Lucas decided to move every test job off Blacksmith. `linux-tests` now runs on
`ubuntu-24.04` and the Windows jobs on `windows-2025`. Both are standard
GitHub-hosted runners, so runner compute is $0 for this public repository.
The Blacksmith rates below stay for accounting of runs before the change.
Billing calibration for that period: Blacksmith charged per started minute for
each job that got a runner (CSL-255).

## Keep development cheap without weakening qualification

- Use local focused tests and existing inexpensive development checks while a
  change is moving. Batch publication; keep new PRs draft until the owner releases
  the hold. Draft/skipped/blocked checks are not qualification PASS.
- Before another push, read the branch's running/queued Actions and coordinate
  with its single writer. Let a paid matrix finish on its pinned candidate; do not
  push over it just to refresh documentation or base state. Necessary corrections
  require explicit owner coordination and new exact-head evidence. Cancellation
  cannot refund elapsed runner time.
- Reserve full expensive qualification for a settled integration candidate using
  the existing authorized path. Preserve exact head/base/merge/tree validation,
  independent readiness, fail-closed results and every required platform cell.
  A manual diagnostic is not independent candidate admission.
- Retain ordinary checks and main/master post-merge fallback. This document adds
  no scheduler, no permission, no settings change and no journal activation.
  Candidate intent caps in the staged #226/#234 source are local safety bounds
  in staged code, not active enforcement here and not monthly billing caps.
- Additional qualification outside this repository stays outside automatic
  nightly expansion until separately priced in CSL-221. Do not allocate the
  entire shared envelope to this repository; reconcile other workloads in CSL-255.

## Price basis and limitations

Extraction source: Recursion #234 head
`df61731a1012e24f0a521ba548c43ce6c6f71ec4`, originally stacked after blocked
#226 and originally inspected against #226
`bc5299953fde3eee1efcc5d2cc4b20884570d0bb`. Lucas approved separately publishing only `tools/ci_runner_cost.py`,
`tools/test_ci_runner_cost.py` and this document on a new branch against master
baseline `e7694742cc97718519f45cd3721c7a2be2924830` (inspected 2026-10-20, after
#240 and #243 landed). This extraction imports no #226 implementation, adds or changes no
workflow, and does not close or supersede #234, which remains open and
unmodified. The rates below keep their original 2026-10-17 check date; this is
a source baseline, not evidence that staged candidate controls are active.

| Runner / scope | USD per minute used for planning |
| --- | ---: |
| `blacksmith-2vcpu-ubuntu-2404` | 0.004 |
| `blacksmith-2vcpu-windows-2025` | 0.008 |
| Standard GitHub `ubuntu-latest` / `ubuntu-24.04` / `windows-2025` / `macos-latest`, this public repo | 0 runner compute |
| Other/larger/custom runners, including paid macOS | Unpriced in core ledger |

[Blacksmith pricing](https://www.blacksmith.sh/pricing) checked 2026-10-17 lists
the Linux and Windows rates above. [GitHub billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
distinguishes free standard hosted compute for public repositories from paid
larger runners and storage. Public does not make Blacksmith runners free. The
zero core macOS rate must never be copied to paid macOS cells. Older
workflow commentary mentioning a $50 budget or charging standard public macOS
does not define this approved envelope.

The ledger conservatively rounds each started job up to a whole minute (minimum
one). Timestamp duration times these rates is only an estimate; provider rounding,
provisioning, invoice adjustments and account credits must be reconciled against
actual receipts. Never deduct advertised free minutes from both repositories or
assume they are available on this account. Storage/cache/artifact charges, other
workloads are additional and unmeasured. Existing Claude workflows use
subscription OAuth; no model/API price or new model billing is authorized here.

## Forecast every start

The supplied measured example was **$0.024 for one Recursion full CI run**. This
is a runner estimate, not an invoice, fixed price or monthly forecast. Read-only
job exports retrieved 2026-10-17 reproduce this amount with this helper:

| Diagnostic run (attempt 1) | Rounded runner estimate |
| --- | ---: |
| [Recursion CI 39067655817](https://github.com/CSL-Laboratories/recursion/actions/runs/39067655817) | $0.024 |

This is historical diagnostic evidence, not current-candidate qualification. It
excludes ordinary pushes, post-merge fallback, storage and other workloads; do
not multiply it by final PR count and call that total cost. The combined
cross-repository forecast and other diagnostic receipts remain in CSL-255.

For a prospective month use the formula below. It applies only to Blacksmith runs before
CSL-408. Runs after CSL-408 use standard GitHub-hosted runners, so their runner compute is
$0 for this public repository, and the forecast for them is $0 plus any other paid workload.

`repository core = 0.024 * Recursion full runs + other paid starts`

`other paid starts = 0.004 * other rounded Linux minutes + 0.008 * other rounded Windows minutes`

The categories must be disjoint. Include opened/synchronize/reopened/ready events,
ordinary pushes, label/manual reruns, merge groups, post-merge fallback, nightly
jobs and retries. Count failed, cancelled, superseded and partially completed paid
jobs. A started matrix contributes its actual executed cells even if its aggregate
fails; never count a run both as a full-pair estimate and as individual jobs.

Illustration only, not a dispatch allowance or measured volume:

| Disjoint planned category | Assumed volume | Estimate |
| --- | --- | ---: |
| Reserved planned work outside this repository | See shared CSL-255 forecast | $30.40 |
| Recursion settled full runs | 300 × $0.024 | $7.20 |
| Ordinary pushes and post-merge fallback | 3,000 Linux + 1,000 Windows rounded minutes | $20.00 |
| Failed/cancelled work and retries outside the above | 500 Linux + 200 Windows rounded minutes | $3.60 |
| Planned core compute | | **$61.20** |
| Headroom to $100 for variability and excluded charges | | **$38.80** |

Replace the assumed volumes with the exported jobs and provider receipts. If the
forecast exceeds $75 core or the all-in projection exceeds $100, coordinate with
Lucas before additional optional qualification; do not silence required checks
or relabel blocked work PASS to fit the envelope. No automatic enforcement exists.

## Reproducible offline ledger

`python tools/ci_runner_cost.py ledger.json > estimate.json`

The helper has no network access, dispatch or status-write path. It reads a JSON
array of normalized GitHub Actions job exports. Keep the original API responses
beside the ledger and preserve their run/attempt/page inventory. Each entry is:

```json
[
  {
    "repository": "CSL-Laboratories/recursion",
    "run_id": 100,
    "run_attempt": 1,
    "job_id": 200,
    "event": "push",
    "workflow": ".github/workflows/ci.yml",
    "name": "linux (3.12)",
    "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "labels": ["blacksmith-2vcpu-ubuntu-2404"],
    "started_at": "2026-10-17T00:00:00Z",
    "completed_at": "2026-10-17T00:01:01Z",
    "conclusion": "success",
    "runner_id": 123,
    "steps": [{"name": "test", "conclusion": "success"}]
  }
]
```

This synthetic example estimates $0.008. Map repository plus the run API's `id`,
`run_attempt`, `event`, `path` and `head_sha`, and the job API's `id`, `name`,
`labels`, `started_at`, `completed_at`, `conclusion`, `runner_id`, `steps`. Use actual attempt-specific
responses, not the latest attempt copied onto earlier jobs. Preserve labels as
exported; unknown/ambiguous runner labels are unpriced, never guessed free.

For a month, enumerate all workflows/events and all pages of repository Actions
runs, then **every attempt 1 through run_attempt** and all job pages at
`GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs`.
Include runs crossing month boundaries; retain the full timing and reconcile the
provider's monthly allocation separately. Do not filter to successful runs,
latest attempts, PR heads or candidate dispatches. Record unavailable/deleted
runs, API limits and incomplete pages as collection gaps; the helper cannot
detect omitted jobs or establish organization-wide completeness.

The output preserves per-job identity/event/outcome and attempt links, records the
input SHA-256, deduplicates identical repeated job exports, and rejects conflicting
duplicates. Unknown runner/timing records produce a known subtotal but **no total**
for the supplied jobs (exit 1); malformed records fail (exit 2). A skipped job is
zero only with no assigned runner (`runner_id` null/0) and no steps. GitHub can
stamp timestamps even on these unstarted jobs; these are not executed coverage.
Other jobs with timing are counted regardless of outcome. Exit 0 means only all supplied rows
were priced; it does not prove complete monthly usage, billing, CI PASS or budget
compliance. Store receipts separately and reconcile invoice credits once at the
account level, documenting differences rather than altering exported evidence.

Regression gate: `python -m unittest discover -s tools -p test_ci_runner_cost.py`.
