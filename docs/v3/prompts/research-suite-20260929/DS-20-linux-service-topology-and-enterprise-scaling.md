---
title: DS-20 — Linux service topology and enterprise scaling
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-20 — Linux service topology and enterprise scaling

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Produce the evidence needed for the Owner to decide how Recursion v3 is provisioned and scaled on Linux — daemon topology, systemd unit/users/groups, resource budgets, single-authority multi-node operation, and enterprise deployment — before Milestones 2, 7, 8 and 9 commit to a deployment shape. This is blocking because the current deployment already runs several processes as different users that contend for one lock (`esc-0013` in `state/INBOX.md`), because `docs/v3/ARCHITECTURE.md` expects one daemon per authority domain/OS account, and because multi-node enrollment, split-brain and offline revocation must be designed before any node fabric is built. Nothing here selects a runtime, store, consensus library, container runtime or cloud. This is research only with an explicit no-deployment rule; procurement/compliance research carries no certification claim.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. **DO NOT select a language, store, framework, renderer or plugin runtime.** The Owner decides. No claim that a distro, kernel, systemd version, SELinux policy or FIPS module is certified follows from this file. `state/INBOX.md` and `state/DECISIONS.md` are read-only here and may not be edited; existing escalations are cited, never resolved by this lane.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/ARCHITECTURE.md`
- `docs/v3/ARCHITECTURE-OPTIONS.md`
- `docs/v3/ROADMAP.md`
- `docs/v3/SECURITY.md`
- `docs/v3/STATE.md`
- `docs/v3/PERFORMANCE-PLAN.md`
- `docs/v3/TELEMETRY.md`
- `docs/v3/CONSTRUCTION-RUNBOOK.md`
- `deploy/sysusers.d/recursion.conf`
- `deploy/tmpfiles.d/recursion.conf`
- `deploy/systemd/recursion-backup.service.example`
- `deploy/systemd/hermes-actuator.service`

## Research questions

### Daemon topology, units and ownership

1. What daemon topology implements "one daemon per authority domain/OS account" (`docs/v3/ARCHITECTURE.md`): one unit, one unit per role, or one unit per account/domain — and how does it keep exactly one canonical writer?
2. How should `deploy/sysusers.d/recursion.conf` and `deploy/tmpfiles.d/recursion.conf` evolve — what users/groups, `RuntimeDirectory`, `StateDirectory`, `LogsDirectory`, and what directory modes — so the documented root-run backup and the non-root actuator can both be correct rather than in conflict?
3. Given the open escalation `esc-0013` (root-first `state/.locks` at mode 0700 permanently locks out non-root participants; `backup-offsite.py` runs as root while the actuator runs as `recursion-actuator`; no shared group exists), what are the competing resolutions — a shared group with `2770`, a differently-owned-but-safe `.locks`, or running the backup non-root — and what does each cost to the trust model?
4. Where do `state/`, `/opt`, `/var/lib`, `/var/log` and XDG locations belong for a root-owned `/opt/recursion` checkout, and how is the code/config/state/credential split kept from silently collapsing into one writable tree?

### Scaling and topology

5. Vertical vs horizontal scaling for the coordinator: what exactly scales with CPU/RAM (event append, projections, observers), what does not (the single-writer admission path), and where does the 2-vCPU/4-GiB coordinator-small profile become the limit?
6. Single-authority multi-node operation vs hosted multi-tenancy: what changes in the authority plane, privacy/egress and isolation when one authority spans several nodes versus several tenants share one service?
7. How are enrollment, trust-root/operator separation, signed envelopes and the lightweight preflight (`docs/v3/SECURITY.md`) sized for an enterprise fleet, and what is the minimum honest preflight that completes in seconds with zero model tokens?

### Coordination and failure

8. Leader election and fencing: what incarnation/fencing-token design guarantees a stale holder cannot authorize a transition, and how do `etcd`/Raft, an embedded consensus library, or a single durable lock each fail under partition and clock skew?
9. Split brain with a sleeping laptop or a network partition: what is the required behavior for a node that wakes holding a stale lease, and how is "finish only the last bounded local operation and spool signed evidence" enforced without allowing a disconnected publish?
10. Offline revocation, duplicate delivery and spool/spool-reconcile: what protocol makes re-admission after reconnect revalidate GoalSpec/fence/lease/PAUSE before accepting spooled evidence or any derived action?
11. Consensus necessity: when is consensus genuinely required (multi-writer coordination, membership) versus avoidable (single-writer authority + outbox), and what is the failure mode of adopting consensus where it is not needed?

### Enterprise install and security

12. Enterprise distros (RHEL/Ubuntu/Debian), SELinux vs AppArmor policy, FIPS-mode crypto constraints, and air-gapped installs — what breaks, and how is each denial made visible rather than swallowed?
13. Proxies, DNS/TLS, and certificate management for node and provider egress: what must be configurable per site, and where does a corporate TLS-inspection proxy silently break pinning or mTLS?
14. Observability in an enterprise: `journald` vs OpenTelemetry vs Prometheus for the signals in `docs/v3/TELEMETRY.md`, and how is telemetry kept local/minimal without leaking private membership?
15. Backup and restore operations: `restic` vs `borg` vs ZFS snapshots for the state+artifact set, consistency of a snapshot taken during a live append, and key/credential exclusion?

### Containers and orchestration

16. Containers (`podman`/`docker`) and Kubernetes: when is the daemon a `StatefulSet` vs a custom operator, how are PVs and network policy used for one-authority state, and where does secrets injection conflict with the "no secrets in state/logs" rule?
17. Rollout/rollback in an orchestrated environment versus the mode table in `docs/v3/ARCHITECTURE.md` (`interrupt_save_update_resume` etc.): what does the platform do that the update contract forbids, and how is that fenced?

### Capacity and SLO

18. Capacity, queueing and SLO modelling: what is the admission equation (`PERFORMANCE-PLAN.md`) on a real Linux host, what load-shedding order is safe (analytics/indexing first, integrity last), and what watchdog and bounded-downtime behavior is acceptable?
19. Procurement/compliance as research only: which claims (auditability, data residency, deletion proof) can be supported by the design, and which must be explicitly `unknown` or `requires-certification` with no claim made here?

## Deliverable

A source-pinned report containing: a claim table (claim, source, version/date, classification, confidence, expiry trigger); a topology and ownership matrix (units, users/groups, directories, modes) with every cell measured, modelled, or `unknown`; a fault matrix for partition, sleeping laptop, duplicate delivery, offline revocation and split brain; explicit cost/scale estimates with denominators (per node, per 100 identities, per event/sec); an enterprise-install feasibility table with no certification claim; a recommendation tied to a named competing option and decisive falsifier; and open Owner questions. No implementation is selected as a product default and nothing is deployed.

## Non-goals

- No deployment, install, container build or cluster creation.
- No systemd unit, `sysusers.d`/`tmpfiles.d` or inventory change.
- No selection of an orchestrator, consensus library, container runtime, backup tool or observability stack.
- No provisioning decision written into `state/INBOX.md`, `state/DECISIONS.md` or any protected path.
- No resolution of `esc-0013` or any other open escalation; they are cited as inputs only.
- No procurement, compliance or certification claim.
- No provider, model, DNS, registry or paid network calls.
- No numeric SLO, budget or resource ceiling invented before measurement.
- No modification of `CONTROL_PLANE.md`, `ANCHOR.md`, `state/**` or `scripts/`.

## Completion gate

- Every systemd, `sysusers.d`, `tmpfiles.d` and directory-mode claim is pinned to a specific version/date or marked `unverified`.
- The `.locks` multi-user problem is analysed against the actual documented deployment (`recursion-backup.service` as root, `hermes-actuator-state.service` as `recursion-actuator`, the four declared users/groups) with the competing resolutions and their trust costs.
- Split brain, sleeping laptop, duplicate delivery and offline revocation each have a specified behavior and a falsifier.
- Consensus is justified only where required, with the no-consensus design stated as the competing option.
- Enterprise install claims (SELinux/AppArmor, FIPS, air-gap, proxy/TLS) carry no certification claim and name their failure mode.
- Every recommendation names a competing option and a decisive falsifier, and preserves an explicit no-selection/no-deployment outcome.
- Observability, backup/restore and container/Kubernetes sections tie to `docs/v3/TELEMETRY.md`, `docs/v3/STATE.md` and the `docs/v3/ARCHITECTURE.md` update modes.
- Protected paths are untouched; privacy and secret scan are clean.
