---
title: DS-25 — Update, release, rollback and disaster recovery
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-25 — Update, release, rollback and disaster recovery

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Decide the exact update-mode, activation-receipt, release-identity, schema-migration, backup/restore and disaster-recovery contract that satisfies `OR-033`'s six update modes and four separate restoration outcomes, and the `E-FHAR2-108` release/boot/migration minimum. It is blocking because a wrong disposition can silently overwrite newer work, claim a rollback of an already-accepted external effect, or make optional code a boot/recovery dependency, and because `OR-019` requires the versioned release to recover canonical state with every optional adapter absent.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This lane performs no deployment, no publication, no self-update, no version installation and no restore; it selects no storage engine, packaging format, signing stack or update channel. `OR-033` preserves all six modes and does not authorize this agent to update itself or change its configuration. Restoring an old executable is one strategy, not universal state or task recovery, and no rollback may be reported as undoing an external effect.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/CONSTRUCTION-RUNBOOK.md`
- `docs/v3/CURRENT-AND-MIGRATION.md`
- `docs/v3/ACCEPTANCE.md`
- `docs/v3/ROADMAP.md`
- `docs/v3/STATE.md`
- `docs/v3/ARCHITECTURE.md`
- `docs/v3/PLUGINS-AND-EDITIONS.md`
- `docs/v3/SECURITY.md`
- `docs/v3/FRONTIER-HARNESS-RESEARCH-TRANCHE-2-EVIDENCE.md`
- `docs/v3/foundation-research/mark2/handoff.md`
- `docs/v3/DECISION-REGISTER.md`
- `docs/v3/construction/replay-store-evidence-20260907/dossier.md`

## Research questions

### Update modes, restoration outcomes and configuration publication

1. For each of the six update modes — (1) interrupt, save, update, resume; (2) interrupt, update, stop tasks; (3) wait until current tasks finish; (4) auto-update off; (5) notify and escalate to the Owner; (6) stop everything and wait until the update lands — what exactly are "current tasks", "everything" and "lands" defined as observable behavior, and which mode is distinguishable from every other under fault?
2. How are the four restoration outcomes — working software, readable history, preserved newest work, and resumable tasks — measured separately, and what negative trial shows restoring an old executable fails to recover one or more of them?
3. For the accepted starting recommendation (verified compatible rollback preserving newest work/history, then verified resume or hold), what is the exact comparison against repair-forward, component isolation, waiting and separate-instance restoration, and what contrary evidence would justify a proposed revision?
4. How does expected-revision configuration publication reject a stale write, and how are the accepted and applied revisions recorded as separate receipts so a readback is never mistaken for runtime adoption?
5. How are the two configuration-propagation choices (outstanding sessions versus new/isolated sessions only) represented, how do active, queued, idle and suspended sessions, concurrent spawns and descendants of older sessions behave under each, and how does a behavioral change under an unchanged protocol or schema get detected and disclosed given SemVer alone cannot express schema, constitution or artifact compatibility?

### Staging, activation receipts and landing observation

6. What exact staged/quarantine/promote/activation lifecycle and receipt fields bind a candidate to its staged artifact, quarantine reason, promotion authorization, activation event and observed landing on each host?
7. What is a per-host activation receipt and landing observation, how does boot detect that an activated version never landed or that a host runs an unknown version, and how is a partially activated or half-updated fleet represented so dependent mutation is blocked?
8. How do PAUSE and the three Stop modes compose with each update mode so an update cannot become an escape hatch around an admission epoch change, and how are retained compatible instances and retained slots used for rollback or isolation while proving a retained instance cannot become a second canonical writer?

### Release identity, authenticity, support windows and emergency updates

9. What exact `ReleaseId` fields (product SemVer, protocol major/minor, schema epoch, constitution-policy digest, core artifact digest, dependency lock digest, build provenance digest, SBOM digest, edition manifest digest) are required, and how is each verified independently rather than collapsed into one version number?
10. What is the required authenticity chain from release manifest to reproducible build to SBOM (proposed SPDX 3.0.1 canonical, CycloneDX derived export), and what does a bit-for-bit reproducible build prove versus what it does not prove about semantic safety?
11. How is `BootReceipt` defined to bind release identity, manifest/lock/core digests, state/checkpoint digest, event high-water, recovery disposition and each optional capability as `absent`, `disabled`, `incompatible`, `unverified`, `quarantined` or `active`, and how does the kernel boot, replay, reconcile leases/effects/PAUSE and report owner status with every provider, engine, plugin and frontend absent?
12. What support window is credible per release, how are protocol/schema/constitution compatibility classes and their expiry communicated without implying an unsupported guarantee, how do emergency security updates differ in staging, notification, deadline and rollback without bypassing ordinary authenticity and landing checks, and how are revoked or expired release metadata, signatures and SBOM entries refused fail-closed at boot?

### Migration, schema evolution and forward-only effects

13. For migration, how is the expand/contract pattern applied per aggregate (additive expand, dual-read, backfill, cutover, contract) with a frozen legacy-state import, shadow comparison and exactly one writer established before cutover?
14. Which effects are forward-only, and how does a destructive schema contraction or an incompatible checkpoint force repair-forward, isolation or a separate instance instead of a false rollback claim?
15. How are retained slots, repair-forward and isolation compared for an offline host, a torn migration and a disk-full condition, and what is the exact rule once any real or ambiguous external effect has occurred?
16. How does a missing optional capability block only dependent mutation rather than auto-installing, substituting a route, spending or becoming a pass?

### Backup, restore and disaster recovery

17. What defines a consistent snapshot of the selected event store plus immutable artifacts, and how is consistency proven when a writer is active?
18. What is the encrypted offsite backup contract, and how are keys and credential references excluded from the backup or separately provisioned so a restored host gains no authority it should not?
19. How is a fresh-host restore performed outside any VHDX or provider-managed volume, and what restore verification (replay parity, projection rebuild, migration state) must pass before the host is admitted?
20. For torn write, disk full, process death, duplicate delivery and stale lease injection at every staging, activation, backup and restore boundary — and for remote-spool reconciliation after controller recovery — what is the expected recovery disposition, how are realistic RPO/RTO bounds established only after measurement, and how are failed or unknown restore outcomes retained rather than expired or refunded?

## Deliverable

A source-pinned report containing: a claim table (claim, source, version/date, classification, confidence, expiry trigger) for every `MKR-015`-class update and recovery claim; a six-mode by four-restoration-outcome matrix with unknown cells explicitly `unknown`; a staged/quarantine/promote/activation receipt and landing-observation specification; a `ReleaseId` and `BootReceipt` field table with authenticity chain; a support-window and emergency-update policy; an expand/contract migration map per aggregate with forward-only effect boundaries; a backup/restore/DR plan with consistent-snapshot, offsite encryption, fresh-host restore, key exclusion, fault injection, spool reconciliation, RPO/RTO and restore verification; and a rollback-versus-state-recovery analysis. Include a recommendation tied to a named competing option (compatible rollback versus repair-forward versus separate-instance) with a decisive falsifier plus open Owner questions.

## Non-goals

- Do not deploy, publish, install, restore, or self-update anything.
- Do not select a storage engine, packaging format, signing stack, update channel or SBOM tool.
- Do not reopen `OR-033`, `OR-032` recovery arithmetic, or any accepted decision.
- Do not claim a universal update preset, complete restoration guarantee, or rollback of an external effect.
- Do not present a numeric RPO/RTO or support window as ratified before measurement.
- Do not make optional code a boot/recovery dependency or auto-install a missing capability.
- Do not merge two writers or allow a retained instance to become canonical.
- Do not include private project content, transcripts, local paths, credentials or account identifiers.
- Do not treat a package import or a successful startup as evidence of recovery.

## Completion gate

- All six update modes remain distinguishable, including modes 2 and 6, with observable definitions of tasks, everything and landing.
- The four restoration outcomes are assessed separately, and restoring an executable is never reported as full recovery.
- Expected-revision publication has accepted-versus-applied receipts and a stale-write falsifier.
- Activation receipts and per-host landing observation exist, with a partial-activation block rule.
- `ReleaseId`, `BootReceipt` and the authenticity chain are field-level and reproducible-build limits are stated.
- Migration uses expand/contract with forward-only effects, destructive contraction and retained-slot/repair-forward/isolation alternatives.
- Backup/restore/DR covers consistent snapshot, encrypted offsite backup, fresh-host restore outside any VHDX, key exclusion and remote-spool reconciliation.
- Fault injection covers torn write, disk full, process death, duplicate delivery and stale lease at every boundary.
- Revoked or expired metadata and emergency updates fail closed.
- No protected path is edited, no deployment or publication occurs, and open Owner questions are listed separately.
