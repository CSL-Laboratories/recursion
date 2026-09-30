---
title: DS-04 — Plugin trust and supply chain
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-04 — Plugin trust and supply chain

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

This research informs what a maintainer signature actually proves and how unsigned local plugins can keep full expressiveness without silent supply-chain failure, so the plugin trust and grant contract can be frozen. It is blocking because `OR-007` ratifies a maintainer-signed trust root and digest-aware acknowledgement while leaving the relationship between one acknowledgement and separate quota, spending, route, update and revocation grants unresolved. A remote maintainer must never gain veto over a local owner's installation.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This lane may not install, sign, publish or update any plugin, and it must not treat a signature, SBOM or transparency record as proof of benign runtime behavior or of spending entitlement.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/RESEARCH-AGENDA.md` (lane R6)
- `docs/v3/PLUGINS-AND-EDITIONS.md`
- `docs/v3/SECURITY.md`
- `docs/v3/ACCEPTANCE.md`
- `docs/v3/EXECUTIVES.md`
- `docs/v3/DECISION-REGISTER.md`
- `docs/v3/CONSTITUTIONAL-AMENDMENT-PROPOSAL.md`

## Research questions

1. What does maintainer signing by the installation trust root actually prove about publisher/review identity, and what does it not prove about runtime behavior, project fit or quota entitlement?
2. How can unsigned local plugins retain full feature expressiveness after a digest-specific warning/acknowledgement rather than warning-only supply-chain failure?
3. Which concrete prior systems (package signing, TUF, SLSA provenance, Sigstore, lockfiles, SBOMs, sandbox/WASI capability models) give enforceable identity, traceability and freshness — with pinned versions and stated limits?
4. Which grant dimensions must stay separate — functional API verbs, OS/workspace scope, network egress, credential access, model launch, route/account, subscription quota, metered spending, update, downgrade and revocation?
5. Can one digest-specific acknowledgement safely mint requested grants, or is staged consent required, and what policy keeps the novice path simple without silent spending authority?
6. How should dependency-confusion, update compromise, rollback and unmaintained-source risks be represented, tested and quarantined without a mandatory remote kill switch?
7. What is the exact reload/update/revocation lifecycle (discover, verify, admit, stage, drain, fence, retire), and how do stale packets, leases and subscriptions fail closed?
8. How is the trust tier (kernel, signed built-in, signed isolated third-party, untrusted view, external engine) kept as an isolation/review requirement rather than an authority grant?
9. What deterministic tests prove that a disabled plugin performs no import, install, process, network, credential, model or quota effect?
10. What prevents a remote maintainer from vetoing, disabling or updating a local owner's unsigned plugin against the owner's configured intent?
11. What competing option (mandatory central registry with remote revocation) fails, and by what decisive falsifier tied to owner sovereignty?
12. How are version support windows, rollback and security-emergency refusal represented as configurable policy rather than silent expansion?

## Deliverable

- A source-pinned claim table classifying each trust/supply-chain mechanism by what it proves, confidence and expiry.
- A grant-dimension matrix showing separate versus shared acknowledgement, with per-dimension falsifiers.
- A lifecycle and failure-containment table for install, reload, update, revocation and quarantine.
- A recommendation tied to a competing option and a decisive falsifier, plus open Owner questions.

## Non-goals

- Do not install, sign, publish, update or execute any plugin or package.
- Do not reopen `OR-007`/`OR-014`, and do not select which dimensions share one acknowledgement.
- Do not introduce a mandatory central marketplace, remote kill switch or maintainer veto.
- Do not treat a signature, SBOM or transparency record as a safety verdict or spend grant.
- Do not edit constitution, auth, config, cron or plugin manifests.
- Do not include private project identity, credentials or local paths.

## Completion gate

- Every trust claim is pinned to a primary source or explicitly marked unverified, with license noted.
- The grant matrix covers every listed dimension separately and shows where sharing is safe or unsafe.
- Every recommendation names a competing option and a decisive falsifier.
- Owner sovereignty is preserved: no design grants a remote maintainer veto over a local owner.
- `OR-007` is neither reopened nor promoted; shared-acknowledgement choice remains an Owner question.
- No protected path is edited and no runtime/provider/auth/cron/PAUSE/spending/install effect occurs.
- The deliverable passes a privacy and secret scan; Owner questions are listed separately.
