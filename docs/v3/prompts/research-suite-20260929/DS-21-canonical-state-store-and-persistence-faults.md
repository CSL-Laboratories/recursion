---
title: DS-21 — Canonical state store and persistence faults
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-21 — Canonical state store and persistence faults

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Produce the evidence the Owner needs to choose the physical event store and its durability profile for the canonical Recursion v3 state boundary — the JSONL/JSON-Sequence + CAS versus single-writer SQLite WAL + CAS comparison named in `docs/v3/STATE.md` and `E-FHAR2-103`, with embedded-KV and Postgres alternatives considered for multi-node. This is blocking because Milestone 2 (`docs/v3/ROADMAP.md`) delivers the canonical writer and must beat the JSONL baseline on a crash/replay/fault matrix before SQLite is adopted, and because the store must never silently become a second authority system. Nothing here selects a store; the comparison stays open and technology-specific.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. **DO NOT select a language, store, framework, renderer or plugin runtime.** The Owner decides. No claim that a current upstream database version (for example the named SQLite WAL-reset bug through 3.51.2, fixed in 3.51.3) is deployed, certified or safe follows from this file. Storage serialization is not Recursion authority (`E-FHAR2-103`).

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/STATE.md`
- `docs/v3/ARCHITECTURE.md`
- `docs/v3/ARCHITECTURE-OPTIONS.md`
- `docs/v3/PERFORMANCE-PLAN.md`
- `docs/v3/RESEARCH-HYPOTHESES.md`
- `docs/v3/SECURITY.md`
- `docs/v3/ROADMAP.md`
- `docs/v3/FRONTIER-HARNESS-RESEARCH-TRANCHE-2-EVIDENCE.md`
- `docs/v3/CONSTRUCTION-RUNBOOK.md`

## Research questions

### Candidate set and shape

1. For the canonical event/projection boundary, what is the exact current (pin by version/date) status of each candidate — append-only JSONL/JSON Sequence + content-addressed store; single-writer SQLite WAL + CAS; embedded KV (LMDB, RocksDB, sled, redb); a log-structured/hybrid design; and Postgres for a multi-node authority — on durability, concurrency, corruption recovery and operational cost?
2. Which candidate can honestly satisfy "one canonical writer per aggregate with a monotonically increasing sequence" plus "one projection high-water and digest", and where does a candidate introduce a second writable truth that `docs/v3/STATE.md` forbids?

### Durability and fault behavior

3. Durability profiles: how do `PRAGMA synchronous=FULL/NORMAL/OFF`, WAL checkpointing (`wal_autocheckpoint`, `PRAGMA wal_checkpoint`), group commit, and the exact `fsync`/`fdatasync` placement differ, and what is lost at each level on power loss versus process death?
4. Torn and partial writes: what does each candidate leave after a crash before append, after append before flush, after flush before rename/replace, after `fsync` of the file but before directory `fsync`, and after a checkpoint — and does recovery refuse silently or choose a winner?
5. Disk-full and allocation failure: what happens to each candidate at each allocation point, does it corrupt the tail, and how is the failure surfaced as a typed refusal rather than a truncated history?
6. What is the current documented state of the named SQLite WAL-reset bug (through 3.51.2, fixed in 3.51.3 and named backports), how does a stale `-shm`/`-wal` or a reset WAL read on an older build, and how does the version pin become a revalidation/fault-test trigger rather than an inferred deployment fact?

### Concurrency and locking

7. Single-writer and MVCC semantics: `SQLITE_BUSY`/busy-timeout retry behavior, WAL reader/writer concurrency, LMDB's single-writer/multi-reader design, and where a language-level lock fails to serve as an epoch fence across processes and OSes?
8. `mmap` usage and its hazards: memory-mapped DB pages, coherence after an external writer, truncation/`SIGBUS`, and how `PRAGMA mmap_size` interacts with reader lifetime and checkpoint starvation?
9. Cross-OS locking: SQLite's Windows vs POSIX VFS locking, LMDB's POSIX-only assumptions, advisory-vs-mandatory lock behavior, and behavior on SMB/NFS — which candidate silently weakens a single-writer guarantee on a network filesystem?

### Schema, migration and exactly-once

10. Schema evolution and migration: expand/contract patterns, `PRAGMA user_version`, forward/rollback limits, and how a projection rebuild from durable events plus CAS artifacts is proven equal without being a second authority?
11. Outbox and exactly-once: how does the store participate in the `State.md` prepare/execute/reconcile and idempotency-key contract so a duplicate command returns the prior outcome and a stale epoch fails without a new event?
12. Projection vs replay parity: why does equal rendered state not prove equal event order, crash behavior or writer exclusion (`docs/v3/STATE.md`), and what test separates the two?

### Performance and growth

13. Event throughput and latency: what are the prepared-statement, batch-append, index and page-cache behaviors for each candidate on the `E-FHAR2-102` workload (10,000 and 100,000 events, 4 read observers) and the `PERF-001` 1M-event replay, with p50/p95/p99 append, busy time and checkpoint cost?
14. Growth and compaction: WAL growth and starvation, `VACUUM`/incremental-vacuum, LMDB map-size and page reuse, RocksDB compaction stalls, and how DB size tracks metadata while payload bytes stay in CAS (`PERF-002`)?
15. fsync amortization: group-commit batching versus latency, and the exact point where batching violates a durability profile that a later milestone promised?

### Backup, integrity and privacy

16. Backup/restore: consistent store + artifact snapshot, `VACUUM INTO` vs file copy vs `restic`/`borg`, restore onto a fresh host, and forward migration plus rollback where schema permits?
17. Integrity: `PRAGMA integrity_check`/`quick_check`, per-frame CRC/length and hash chaining, CAS digest verification, and how the store detects a corrupted artifact reference and fails closed?
18. Encryption at rest and key rotation: SQLCipher or filesystem encryption, DEK/KEK rotation without breaking content identity, and how CAS GC/eviction and dedup interact with the scoped-deletion and privacy-leakage rules in `docs/v3/STATE.md`?
19. CAS GC and content-addressing: reachability-rooted GC, eviction order under free-space floors, dedup across projects (forbidden for private content), and where a global digest becomes a membership oracle?

### Fault matrix, platforms and selection

20. What is the per-write-stage crash fault matrix — crash before prepare, after prepare, during append, after append before flush, after flush before projection commit, after projection before outbox, plus partial/torn/corrupt frame, missing/wrong CAS object, disk full, long reader, slow/dead client, duplicate command, stale writer/reader epoch, schema/protocol mismatch and migration rollback — and which failures are fuzzed?
21. Windows/Linux VFS differences and network filesystems: locking, `FlushFileBuffers`/`ReplaceFileW` vs `fsync`/`rename`, and which results cannot be claimed on the other OS or on SMB/NFS?
22. Selection criteria and falsifiers: what is the decision rule (hard floors first — replay/recovery/privacy — then resources), what is the decisive falsifier for each candidate, and how does `E-FHAR2-103`/`EXP-FHAR-11` (count all bytes, identical CAS) bound the comparison?

## Deliverable

A source-pinned report containing: a claim table (claim, source, version/date, classification, confidence, expiry trigger); a store comparison matrix whose every cell is measured, explicitly modelled, or `unknown`; a per-write-stage crash fault matrix with recovery disposition per cell; explicit throughput/latency/size estimates with denominators (per event, per observer, per 1M events, total store bytes); a recommendation tied to a named competing option and decisive falsifier; and open Owner questions. No implementation is selected as a product default.

## Non-goals

- No selection of SQLite, JSONL, LMDB, RocksDB, sled, redb or Postgres as a product default.
- No database created, migrated, benchmarked or fault-injected against live state.
- No claim that a current DB/runtime version is certified or that the named WAL bug is present or fixed in the deployment.
- No numeric performance or size budget invented before measurement.
- No reopening of the single-canonical-writer, privacy or scoped-deletion rulings.
- No provider, model or network calls.
- No modification of `docs/v3/STATE.md`, `docs/v3/PERFORMANCE-PLAN.md` or any protected path.

## Completion gate

- Each candidate is pinned by exact version/build/compile options/date, or its evidence is explicitly `unverified`.
- The crash fault matrix covers every write stage in question 20, and each cell states a recovery disposition or `unknown`.
- All store bytes are counted together (`E-FHAR2-103`/`EXP-FHAR-11`); moving bytes out of SQLite is not itself a win.
- Durability cells name the exact `synchronous`/checkpoint/fsync setting and the OS/filesystem they were observed on.
- Cross-OS and network-filesystem limits are stated, and no POSIX-only result is claimed for Windows or SMB/NFS.
- Privacy, key rotation and CAS GC/dedup claims trace to `docs/v3/STATE.md` scoped-deletion rules.
- The recommendation names a competing option and a decisive falsifier, and preserves an explicit no-selection outcome.
- Protected paths are untouched; privacy and secret scan are clean.
