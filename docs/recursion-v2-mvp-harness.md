# Recursion v2 MVP custom harness

Status: source-only MVP accepted on 2026-08-31; publication and production deployment pending.

This is the capability reference for the custom Recursion harness implemented on
`loop/work/recursion-v2-mvp`. Authority remains defined by `CONTROL_PLANE.md`: Lucas decides,
Hermes plans and supervises, workers execute bounded tasks, independent verifiers gate review,
and Lucas merges and deploys.

## End-to-end contract

```text
goal + policy
  → strict task, budget, capability grant, and assignment records
  → immutable TaskPacket (root → child → grandchild, maximum depth 2)
  → fresh capability, quota, health, network, sandbox, and billing negotiation
  → deterministic placement on an eligible host/capacity pool
  → durable lease + attempt + result journal
  → independent verifier dispatch + authenticated receipt
  → stale/scope/source/gate checks
  → review intent
  → scripts/publish.sh review branch
  → Lucas PR review, merge, credentials, deployment, and restart
```

No model claim is authoritative by itself. Every transition is rederived from exact source,
digests, durable records, and controller-owned evidence.

## Contracts and provenance

The harness validates versioned supervisor, task, capability-grant, budget, assignment, receipt,
checkpoint, verification, recovery, candidate, and evidence records. Its canonical JSON layer:

- rejects unknown/missing fields, cycles, excessive depth/nodes/bytes, non-finite values, and
  malformed timestamps, identifiers, paths, refs, or digests;
- rejects secret-looking content and labelled raw/private prompts, transcripts, conversations,
  or private model output;
- binds every record to its task/source and validates cross-record relationships;
- enforces child scope, path, capability, time, token, concurrency, and cost as subsets of the
  parent authority;
- validates ordered checkpoint chains and exact evidence references.

`TaskPacket` objects are immutable and factory-issued. A structurally identical copy has no
authority. Packet validation binds the exact object to its canonical snapshot, parent packet,
lineage, source, assignment, budget, grant, selected context, checkpoint, receipt, candidate,
evidence, and limits. Context retention is bounded and evidence-backed rather than transcript
carrying. The orchestrator accepts only depth 0, 1, or 2.

The source boundary also fails closed for the tested module/global/closure monkeypatch attacks.
Python code in one process remains reflectively mutable, so production still needs process
isolation; source attestation is not a replacement for an OS security boundary.

## Provider and execution adapters

Five adapters share one capability and evidence contract:

| Adapter | Roles | Authority and execution posture |
|---|---|---|
| `codex-cli` | implementer, verifier | exact safe `codex exec` argv, OAuth/subscription, workspace-write sandbox |
| `cursor-cli` | implementer, verifier | structured stream output, sandbox enabled, reviewed capability evidence |
| `hermes-api` | implementer, verifier | provider-only network, endpoint and credential references, empty local argv |
| `opencode-openrouter` | implementer, verifier | provider-only network, explicit endpoint/credential refs and enclosing sandbox proof |
| `qwen-local` | implementer, verifier | local billing, measured health, local endpoint and enclosing sandbox proof |

Negotiation checks role, capabilities, model, network, execution mode, affinity, freshness,
expiry, availability, health, executable provenance, sandbox proof, quota, concurrency, billing,
approval digest, and budget. It returns a complete rejection trace rather than silently relaxing
requirements. PAUSE rejects all candidates. Quota exhaustion or missing evidence defers work;
neither enables on-demand spend.

Dispatch plans are immutable, factory-issued, packet-bound, and argv-exact. Dangerous flags,
unsafe worktrees, undeclared endpoint/credential references, injected models, and reconstructed
plans fail closed. Credentials remain references and are excluded from durable public records.

Results normalize into the same task, budget, grant, assignment, checkpoint, candidate, evidence,
receipt, result-envelope, and usage structure across adapters. Result recording checks source SHA,
time window, candidate commit, checkpoint chain, execution mode, plan authority, usage totals, and
result digest. Duplicate delivery is idempotent only when every authenticated field is identical;
conflicting duplicates are rejected. Hermetic Codex and Cursor completions prove this common path
at zero cost.

## Placement and durable execution fabric

The fabric separates engine, provider, capacity pool, model, host, sandbox, network, project, and
publication policy. It supports:

- strict host registration and monotonic, freshness-bounded heartbeats;
- capacity-pool availability, billing basis, account identity, quota, and concurrency;
- manual-pin, subscription-first, local-first, cheapest-eligible, fastest-eligible,
  strongest-reviewer, and weighted-rotation deterministic routing;
- complete eligibility/rejection traces for capability, OS, architecture, sandbox, network,
  endpoint, billing, pool, quota, health, and capacity mismatches;
- atomic admission across threads/processes, host/pool/task reservations, and aggregate budget
  enforcement;
- durable leases, attempts, results, expiry, retry, cancellation/reconciliation state, and restart
  reconstruction without relaunching completed work;
- replay-safe assignment identity and refusal of late, duplicate, stale, mismatched, or resurrected
  results;
- append/replace durability with pinned directories, link/reparse protection, file-identity checks,
  atomic temporary files, reload postconditions, and typed disk failure;
- authenticated verification-receipt storage and key rotation;
- candidate-ref validation, stale-base classification, and serialized same-project integration
  while different projects may proceed concurrently.

The acceptance gate injects `ENOSPC` and proves the failed admission leaves no journal, temporary
file, lease, attempt, or result; restart and retry then produce a durable lease.

## Bounded orchestration and independent review

`MVPOrchestrator` composes packet admission with the durable fabric. It:

- binds requirements to the packet's project, repository, branch, source SHA, allowed paths,
  capabilities, gate, assignment, and role;
- reserves child budgets against exact parent/root authority and tracks sibling aggregate cost;
- checks PAUSE before and during placement, verification, restart recovery, and review admission;
- dispatches implementer and verifier work as distinct durable leases;
- refuses self-verification and requires succeeded durable results from both roles;
- requires a controller-authenticated verification receipt bound to candidate, result commit,
  source, diff, scope, gate, assignment, lease, attempt, and verifier identity;
- creates a review intent only after every packet, durable result, receipt, ref, and integration
  binding is revalidated;
- exposes bounded controller events rather than creating a second source of truth.

Review intent is not publication. Only `scripts/publish.sh` may push the canonical
`loop/work/<task-id>` ref, and it cannot merge a protected/default branch.

## Operator control surface and watchdog

The source-accepted control surface answers from a strict, fresh, content-addressed snapshot:

- `/today` — bounded landing list;
- `/status` — PAUSE state and active work;
- `/why` — recorded rationale;
- `/next` — next eligible work;
- `/project` — exact workstream ref/SHA, readiness, and blockers;
- `/budget` — quota, worker/review capacity, observed monthly spend, and spend posture.

Read commands remain available under PAUSE. `/pause`, `/resume`, and `/approve` require an
authenticated Lucas principal. They emit content-addressed, deduplicated intents; they do not
mutate PAUSE or call an actuator. Resume remains held under PAUSE until an exact one-shot approval
matches one pending intent at the current source and snapshot. Replayed, stale, ambiguous, or
mismatched approvals fail closed.

The second-box watchdog verifies HMAC-signed controller heartbeats with monotonically increasing
sequence numbers, bounded clock skew, exact controller/source/evidence bindings, and key rotation.
It emits at most one death-attention intent per heartbeat episode, including while PAUSE preserves
awareness. Durable state, keys, notification transport, and delivery are deployment seams.

## Cursor Cloud source backend

The existing Cursor Cloud adapter is covered through fake transport. It binds create requests to
an exact 40-hex source SHA, forces isolated branches, disables vendor PR creation, rejects default
and integration targets, persists attempts/sessions/usage, deduplicates creates and cancels,
classifies rate limits/timeouts/host loss/malformed responses, streams with bounded parsing,
downloads advisory artifacts safely, reconciles ambiguous outcomes after restart, and requires
independent verification before returned repository candidates can proceed. Quota exhaustion
defers and never activates on-demand spend.

This is not evidence of live credentials or a production provider call.

## Acceptance gate

Run from a clean review branch:

```bash
python3 -u scripts/tests/recursion-v2-mvp.test.py
```

The committed gate runs contracts, kernel, adapters, orchestration, control surface, execution
fabric, cumulative v2, cross-engine, Cursor fake transport, Python compilation, git diff hygiene,
Codex/Cursor common-evidence probes, literal disk-pressure recovery, source ancestry, forbidden
diagnostic exclusion, required audits, and a clean-before/clean-after invariant.

## Current boundary and remaining work

The harness is source-ready and review-ready. It is not production-complete. Remaining human or
live seams are:

1. enable the existing approved publisher, validate the active registry row and publisher key,
   push the review branch, open the PR, review, and merge;
2. provision production provider and Telegram credentials without placing them in tracked state;
3. wire the control boundary to authenticated intake, canonical outbox/actuator, and production
   Telegram transport;
4. provision the second-box heartbeat key/transport and durable watchdog state;
5. deploy under process isolation, restart services, and run the live outage, quota, recovery,
   duplicate, stale-result, PAUSE, and publication soak;
6. obtain Lucas's explicit approval before broader autonomy or any metered-spend policy change.
