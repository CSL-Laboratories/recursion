# Guarded fixer reliability (CSL-201)

The workflow installs its embedded pre-push guard outside the PR tree. The
guard consumes one push attempt, verifies the advertised remote head and live
PR identity against the locked checkout, and checks that the candidate descends
from that checkout. A moved head, failed read or second push stops the run.
Git still makes the final ordinary push decision; an external writer can race
the last read. Never fetch, merge, rebase or retry from that stale checkout.

The model must run the existing acceptance gates, copy the full output of
`git rev-parse HEAD`, and verify the live PR head after successful push before
posting an addressed receipt. Short SHA values remain invalid. The guard prints
the full candidate but explicitly does not claim the push has succeeded.
Receipt parsing and authentication are unchanged: model prose is not evidence
of completion, and the workflow audit never creates an addressed receipt.

The terminal audit reconciles actual PR review replies against the captured
per-root context using the workflow-owned copy of the existing receipt consumer.
It reports verified addressed receipts separately from human handoffs, missing
receipts, invalid/superseded receipts and changed roots. Full SHA, authenticated
Claude numeric identity, body fingerprint, captured base and remote ancestry are
required, alongside the existing v3 generation and observation context. The audit
loads bounded authenticated observation history and reuses `prepare_observations`
with journal writes refused. Exact previously authenticated reply ID, author ID
and body digest remain trusted after permission loss, just as in eligibility.
Edited/deleted replies, changed generations, invalid or incomplete history and
any required journal advance leave the captured observation unverified. Newly
unauthenticated content never becomes historical evidence. A reachable push
with an abbreviated receipt is reported as invalid; the workflow does not repair
or author that receipt. This is deterministic reconciliation of observed output,
not proof that a model will produce it.

Reconciliation is read-only and does not change eligibility, reservations,
resolution or READY. It checks at most 50 captured roots, five review-comment
pages and five issue-comment journal pages;
all terminal reads share a 40-call/70-second budget with a final live-head check.
Incomplete reads, duplicate snapshots, changed heads, denied access and exhausted
budgets report unverified. Mention runs without captured context say not
applicable. Results describe the observed context, not an atomic current-thread
decision; future runs still recheck resolved/outdated findings and later replies.

A `!cancelled()` step records a terminal audit after model failure, denied commands,
turn exhaustion or missing execution output. It independently checks the guarded
candidate's remote ancestry and posts a PR notice. If the comment API fails,
the job summary retains the audit and the step fails rather than claiming a
posted handback. A 20-minute model-step timeout reserves room inside the existing
30-minute job. The model limit remains 30 turns. Runner loss, whole-job timeout,
hard cancellation and unavailable GitHub can still prevent remote delivery;
this is not an external durable queue or an atomic writer lock.

Failure notices do not suppress findings or reset reservations. A fresh authorized
run must re-evaluate genuine unresolved/current findings, author permissions,
the three-attempt budget and eight-commit guard. Resolved or outdated findings
are never reopened for qualification. No persistent account access, credentials,
paid provider API or expanded model tool allowlist is introduced. The Git hook
configuration exists only in the disposable runner checkout.

This hook is a reliability control for the authorized fixer, not a security
boundary against hostile code executing as the runner user. Existing acceptance
gates execute PR code with that user's filesystem access; an adversarial test
could alter the hook or Git configuration. Edit/Write denials reduce accidental
model edits but do not isolate shell subprocesses. A separately trusted publisher
would require a broader security design; no such isolation is claimed here.

The handback is a PR-level notice to the author. It does not impersonate a Claude
thread reply or make the stuck-thread watcher classify a no-Claude thread as
stuck. Automatic mediation of workflow failures needs a separate actor-trust and
routing decision. The author must relay such blockers to Linear; transient
failures remain eligible for a fresh bounded retry rather than a needs-human
receipt. One audit is intentional even after model success, which alone cannot
prove every per-root receipt was valid.

## Mediation and implementation remain separate

Devin submitted reviews and authorized human requests drive Claude. Codex
READY requests CI only after live reconciliation; MEDIATE requests adjudication.
Neither is a fix receipt. Codex comments cannot impersonate Claude receipts or
consume Claude attempt reservations. Legacy FIX/timeout dispatch stays disabled.
The new tests retain these distinctions, alongside the existing permission,
same-repository, queue and readiness controls. CSL-89 owns resolution policy;
this change does not enable new thread-resolution authority.

## Operational evidence and landing

The local regression suite proves controlled stale-head and post-check races,
actual hook invocation, strict receipt parsing, and terminal audit fallback.
It does not qualify this guard against a live provider or demonstrate a genuine
autonomous remote fix, push and accepted receipt. That requires an existing
eligible unresolved finding and a coordinated single writer. Do not manufacture
events or reopen resolved findings for qualification.

This work uses an isolated branch and verified default base. Landing remains
held by the owner sequence recorded in CSL-201 and the PR. Refresh only this
branch via ordinary base merges and repeat checks then; no rebase or force-push.
Reconcile overlapping CSL-89 draft #197 before landing. Never modify another
owner's lane. Cross-project operational evidence belongs in Linear and the PR.

Sources: [Git pre-push contract](https://git-scm.com/docs/githooks#_pre_push),
[Actions timeout syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idstepstimeout-minutes).

## Independent subscription review follow-up

An independent, tool-disabled Claude Max source review found that failed model
steps masked execution-file reasons and Git remote names could collide with
the hook's maintenance modes. The audit now reads coarse result fields even
after step failure, prioritizes turn exhaustion over recovered denials, and
uses step failure only as a fallback. Install/terminal modes require exactly
one argument; a two-argument Git hook invocation always goes through target
validation. Read-budget and JSON decode failures remain globally unverified
instead of being mistaken for an observation-history change. Regression tests
cover these paths and the exact ordinary-push allowlist (including negative
force/no-verify/config/remote/dry-run controls).

Receipt reconciliation is a current snapshot and can include a valid receipt
that predates this run. It is not attribution of a fix to the current model
invocation. A missing guarded attempt remains unverified; neither local hook
configuration nor the shared runner identity is a hostile-code boundary.
This change does not alter checkout credential wiring or promise resistance
to code that changes hooks or uses the existing runner credential directly.

## Least-privilege API follow-up

The model no longer receives generic `gh api` access: raw API commands are
explicitly denied. The protected workflow helper exposes fixed current-PR
reads (including a fixed paginated thread-state query), issue comments and
review replies. It accepts no arbitrary endpoint, HTTP method, GraphQL query
or mutation, so ref create/update/delete cannot bypass the push guard through
that tool. Comment data comes only from regular `.claude-fixer-replies/NAME.txt`
files under the checkout; traversal, absolute paths and symlinks are rejected.
These files must not be committed. The helper uses the existing credential.

The push hook also validates Git's actual remote URL against this repository's
GitHub URL. Offline tests cover real delete records, empty input, tags, foreign
URLs, fixed API operations and forbidden ref mutations. The terminal step runs
after model success/failure but honors explicit workflow cancellation; runner
loss and cancellation remain delivery limitations. Existing same-user shell
execution is still outside the hook's isolation guarantee.

Removing raw GraphQL access also removes the model's direct thread-resolution
command. It leaves an addressed receipt after a verified fix; the existing
trusted watcher or a human owns resolution. Watcher predicates and citation,
head/check, authentication and READY/MEDIATE boundaries are unchanged.

The URL check accepts the action's existing `x-access-token` HTTPS userinfo
after parsing and binding the actual GitHub host and repository, without logging
credentials. Other hosts/users, queries/fragments and ports remain rejected.
Malformed helper invocations fail without reading stdin or consuming a push.
Reply input opens nonblocking, verifies a regular descriptor and bounds its read;
named pipes and symlinks cannot stall the helper. These cases have offline tests,
including simulated action URL metadata through the real local Git hook fixture.
