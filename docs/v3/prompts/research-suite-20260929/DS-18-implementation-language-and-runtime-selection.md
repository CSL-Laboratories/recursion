---
title: DS-18 — Implementation language and runtime selection
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-18 — Implementation language and runtime selection

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Produce the evidence needed for the Owner to select the implementation language(s) and runtime for the privileged Recursion v3 kernel — the `decode` / `admit` / `reduce` / `derive_work_packet` boundary, the canonical event writer, and the base of the daemon, process broker and adapters named in `docs/v3/ARCHITECTURE.md` and `docs/v3/CONSTRUCTION-RUNBOOK.md`. This is blocking because every later milestone (Milestone 2 canonical state, 3 plugin/route fabric, 7 node fabric, 8 adaptive optimization, 9 soak) bakes in a runtime's concurrency, memory, packaging and fault semantics, and a wrong base is expensive to unwind. Nothing here selects a language; the comparison stays open and technology-specific so the Owner can decide.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. **DO NOT select a language, store, framework, renderer or plugin runtime.** The Owner decides. No claim that any current upstream language/toolchain version is certified, supported, or free of a specific CVE follows from this file. Option 2 (native deterministic kernel with typed adapters) is Owner-ratified in `OR-018` and is not reopened; this lane compares the runtime that satisfies it.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/ARCHITECTURE.md`
- `docs/v3/ARCHITECTURE-OPTIONS.md`
- `docs/v3/PERFORMANCE-PLAN.md`
- `docs/v3/RESEARCH-HYPOTHESES.md`
- `docs/v3/RESEARCH-AGENDA.md`
- `docs/v3/CONSTRUCTION-RUNBOOK.md`
- `docs/v3/FRONTIER-HARNESS-RESEARCH-TRANCHE-2-EVIDENCE.md`
- `docs/v3/STATE.md`
- `docs/v3/SECURITY.md`

## Research questions

### Candidate set and embedding

1. For the fixed kernel API from `E-FHAR2-102`, what is the current (pin by version/date) status of each candidate — CPython 3.11/3.12/3.13 including free-threaded PEP 703 builds; Rust stable; Go; C/C++ for hotspots; Zig; Java/Kotlin; TypeScript/Bun/Deno; and WebAssembly — on determinism, concurrency, memory safety, FFI, packaging and per-op overhead?
2. Which embedding/hybrid topology is viable and what does each cost: one process per role; Python kernel with Rust hotspots via PyO3; Rust kernel embedding Python (PyO3 `auto-initialize` / subinterpreters), Go via cgo; a WASM-sandboxed plugin core; and a single static binary with no interpreted runtime?

### Determinism and canonical bytes

3. Can each candidate produce **byte-identical** output for the `decode`/`admit`/`reduce`/`derive_work_packet` vectors under RFC 8785 (JCS) canonical JSON: integer and float serialization (including IEEE-754 shortest round-trip, `-0`, NaN/Inf rejection), key ordering for map/dict types, duplicate-key handling, and non-ASCII/normalization policy?
4. What are the exact hashing/RNG/time hazards: default `hash()` randomization and dict insertion-order stability across runs and processes; `std::collections::HashMap` iteration order and Go map randomization; RNG seeding and the admission rule that logical time and random values enter only as admitted inputs; wall-clock vs monotonic sources; and Unicode normalization (NFC/NFD/NFKC) and UTF-8/UTF-16 boundary errors?
5. Which candidates guarantee reproducible refusals — a typed, stable error identity and ordering — and which only guarantee a thrown exception whose message drifts across point releases?

### Concurrency semantics

6. GIL vs free-threaded CPython (PEP 703): what is the current thread-safety state of the C-API, `dict`, and popular extension modules under the free-threaded build, and does the single-writer canonical admission path need a real OS lock or is a language lock sufficient?
7. For each candidate: what exactly happens to a shared mutable structure under two concurrent writers (Rust `Send`/`Sync` compile-time rejection and `unsafe` escape hatches; Go data races, the race detector, and the absence of a memory model for `unsafe`; C/C++ UB and sanitizers; CPython/Dict corruption), and how does this interact with the required absence of data races at the admission boundary?
8. Go scheduler semantics: `GOMAXPROCS`, work-stealing, goroutine blocking on syscalls and the cost of a blocked M, channel backpressure and unbounded-queue counterexamples, versus Rust async (tokio) task cancellation pitfalls (drop-not-await), structured concurrency, and CPython `asyncio`/`threads`/`processes` for the process broker.
9. Structured cancellation: which candidates can guarantee that an in-flight operation is cancelled at a defined boundary and that no orphaned task straddles the PAUSE/admission epoch, and what is the mechanism (drop guards, `CancellationToken`, `context.Context`, `asyncio` task groups)?

### Memory model and tail latency

10. Allocator behavior, fragmentation and RSS growth under 24/7 operation for each candidate: glibc malloc vs jemalloc/mimalloc, Go GC target-percentage and `GODEBUG` knobs, CPython pymalloc/arena behavior and its interaction with free-threading, and Rust's absence of a GC but exposure to unbounded arena/leak growth.
11. GC pause distributions and tail latency: Go's sub-millisecond STW claim and its worst-case under heap pressure; CPython reference counting plus cycle-collector pauses; Java's ZGC/G1 pauses; and where each violates a p99/p999 admission-latency budget on the 2-vCPU/4-GiB coordinator-small profile.
12. Memory-safety posture and `unsafe` blocks: for each candidate, the documented unsafe surface, the volume of security CVEs in the runtime itself and in the specific candidate libraries Recursion needs, and how a small kernel bounds the blast radius.

### FFI and interop

13. What are the exact costs and failure modes of calling SQLite, libc, a PTY/ConPTY layer, a crypto library and MCP transports from each candidate: cgo call overhead and `CGOMAXPROCS`/thread interactions, PyO3 GIL acquisition/release per call, Rust `unsafe` FFI with `bindgen`, and the per-call cost of a WASM sandbox boundary?
14. Subinterpreters and plugin loading: CPython per-interpreter GIL (PEP 684) maturity and extension-module compatibility; `dlopen`/`LoadLibrary` plugin ABI stability across runtimes; and WASM component-model capability isolation versus `dlopen` — which can actually enforce the zero-cost-disabled and capability-intersection contracts in `E-FHAR2-105`?

### Build, packaging and distribution

15. Static musl vs glibc linkage, single static binary feasibility, and Windows MSVC vs MinGW/`-libraries` for each candidate: does the candidate produce a reproducible, code-signable artifact, and what is the measured artifact/install size and direct/transitive dependency count?
16. Reproducible builds and supply chain: which candidates can satisfy `E-FHAR2-102`/`E-FHAR2-108` (`ReleaseId`, `core_dependency_lock_digest`, SBOM in SPDX 3.0.1) with an offline dependency closure, and what is the wheel/sdist, `cargo`, or module-size and vendoring story for an air-gapped install?
17. Windows code signing (Authenticode/EV) versus Linux signing, the packaging of the daemon as a systemd unit vs a Windows service, container-image base and size, and how each interacts with the `deploy/` provisioning already present.

### Performance and platform seams

18. Cold start, per-op overhead, syscall patterns and sustained-load tail latency for each candidate on the fixed `E-FHAR2-102` workload (one GoalSpec, 10 plan revisions, 1,000 tasks, 3,000 edges, 10,000 events; scale 100,000 events), plus 24/7 soak behavior — where do RSS, handles, threads and latency drift?
19. Cross-platform parity and the OS seams a runtime must cross: ConPTY + named pipes + Job Objects + `MAX_PATH`/long-path on Windows, versus `epoll`/`io_uring` + PTY + `cgroup v2` + signals on Linux; which candidates expose these natively and which require a C or `unsafe` boundary?

### Security and ecosystem maturity

20. For the **specific** libraries Recursion needs (SQLite bindings, canonical JSON, crypto/signing, PTY handling, MCP, HTTP), what is each candidate's ecosystem maturity, maintenance cadence, license posture and CVE history, and where is the honest `unknown` rather than a reputation claim?

### Evaluation method

21. How does this tie to `E-FHAR2-102` and `EXP-FHAR-10` and the language-neutral `decode`/`admit`/`reduce`/`derive_work_packet` vectors and frozen Windows/Linux hosts, with the hard-floor rule (correctness/reproducibility first, then resources and speed), the paired practical effect, and the explicit adoption/rejection rule?

## Deliverable

A source-pinned report containing: a claim table (claim, source, version/date, classification, confidence, expiry trigger); a comparison matrix whose every cell is **measured**, **explicitly modelled** (with the model stated), or marked `unknown` — never blank-as-zero; a recommendation tied to a named competing option and a decisive falsifier; explicit cost/latency/resource estimates with denominators (per-op, per-event, per-task, per-view) rather than a composite score; and open Owner questions. No implementation is selected as a product default.

## Non-goals

- No selection of CPython, Rust, Go, C/C++, Zig, Java/Kotlin, TypeScript/Bun or WASM as a product default.
- No runtime, daemon, reducer, adapter or benchmark implementation executed.
- No claim that a current upstream version is certified, supported, or CVE-free.
- No provider, model, network or paid route calls.
- No numeric performance budget invented before measurement.
- No reopening of Option 2 or the required small native loop.
- No private hardware inventory treated as design data.
- No modification of `docs/v3/PERFORMANCE-PLAN.md` or any protected path.

## Completion gate

- Each candidate is pinned by exact version and date on both frozen Windows and Linux hosts, or its evidence is explicitly `unverified`.
- Byte-identical determinism is demonstrated or falsified per candidate on the identical language-neutral vectors, including float/`-0`/NaN and map-ordering hazards.
- Every concurrency claim names a data-race or cancellation failure mode and its test.
- Memory and tail-latency cells report denominators and are labelled measured, modelled, or unknown.
- FFI, packaging, reproducibility and SBOM claims trace to `E-FHAR2-102`/`E-FHAR2-108` obligations.
- The recommendation names at least one competing option and one decisive falsifier, and preserves an explicit no-selection outcome.
- Hard floors (authority, safety, privacy, correctness, reproducibility) are checked before any trade-off or speed comparison.
- Protected paths are untouched; privacy and secret scan are clean.
