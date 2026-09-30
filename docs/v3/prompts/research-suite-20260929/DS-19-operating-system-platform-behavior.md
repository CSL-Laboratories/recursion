---
title: DS-19 — Operating-system platform behavior
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-19 — Operating-system platform behavior

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Produce the cross-platform conformance evidence that lets the Owner decide how Recursion v3 must behave on Windows and Linux — the exact OS seams an implementation must cross — before any language, store or process library is chosen. This is blocking because the process broker, canonical state writer, terminal attach/detach, crash recovery and containment contracts in `docs/v3/ARCHITECTURE.md`, `E-FHAR2-104` and Milestones 2, 6 and 7 are all defined in terms of OS primitives (Job Objects, ConPTY, cgroups, PTYs, fsync/rename), and assuming POSIX semantics on Windows (or vice versa) is a correctness and containment defect. Nothing here selects a runtime, store, renderer or process library.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. **DO NOT select a language, store, framework, renderer or plugin runtime.** The Owner decides. No claim that a current OS build, kernel or terminal is certified follows from this file. The detachable-terminal product ruling is fixed; this lane compares mechanisms and may not reopen it.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/ARCHITECTURE.md`
- `docs/v3/TERMINAL-RUNTIME.md`
- `docs/v3/PERFORMANCE-PLAN.md`
- `docs/v3/SECURITY.md`
- `docs/v3/STATE.md`
- `docs/v3/TELEMETRY.md`
- `docs/v3/FRONTIER-HARNESS-RESEARCH-TRANCHE-2-EVIDENCE.md`
- `docs/v3/CONSTRUCTION-RUNBOOK.md`
- `docs/v3/prompts/research-suite-20260929/DS-09-interactive-terminal-and-communication-views.md`

## Research questions

### Process model and containment

1. What is the exact Linux creation/containment path — `fork`+`exec` vs `posix_spawn` vs `clone3(CLONE_INTO_CGROUP)` — and the Windows path (`CreateProcess` with `PROC_THREAD_ATTRIBUTE_JOB_LIST`), and where do process groups/sessions, Job Object child/breakaway limits, and reparenting to PID 1 or `orphan` differ?
2. How is descendant-tree termination actually guaranteed on each OS (Job Object kill-on-close vs PTY session + process group + `cgroup v2`/`pidfd`+`pidfd_send_signal`), and what escape routes remain (Windows breakaway/creation-path escape, Linux cgroup migration, `setsid` escaping, reparented grandchildren)?
3. How does an orphan or descendant-of-descendant get *detected* (not just killed) on each OS, and what is the honest `unknown` when a process escapes the tracked tree or the parent dies before the child is registered?

### PTY / ConPTY

4. Openpty/termios on Linux vs ConPTY on Windows: what are the exact differences in line discipline, canonical vs raw mode, `SIGWINCH` vs console resize events, XON/XOFF and software flow control, and pseudoconsole teardown semantics?
5. How do buffering, CR/LF translation, UTF-8 vs code-page handling, VT sequence parsing, alternate-screen, DSR/DA response and OSC/DCS pass-through differ, and where does a byte-correct capture depend on which layer translates?
6. What are the documented gaps in ConPTY (early-close, resize race, output coalescing) and in the Linux PTY (EIO on master close, hangup ordering) that the broker in `E-FHAR2-104` must treat as transport-not-authority?

### Filesystem semantics

7. Atomic replacement: `rename(2)`/`renameat2(RENAME_NOREPLACE)` and directory `fsync` on Linux vs `MoveFileEx`/`ReplaceFileW` and `FlushFileBuffers` on Windows — where is atomicity guaranteed, where is it only "usually", and what does a crash between rename and directory-fsync leave?
8. `O_APPEND` atomicity vs `FILE_APPEND_DATA`/`FILE_FLAG_APPEND`, `flock`/`fcntl` advisory locks vs `LockFileEx` mandatory locks, and `mmap` coherence and truncation hazards across OSes — which locking primitive survives a stale-lock and a process death?
9. Path and naming reality: case sensitivity (NTFS vs ext4), `MAX_PATH` 260 and long-path opt-in (`\\?\`), reserved device names, alternate data streams, symlinks/junctions, POSIX ACLs vs Windows ACLs, and `umask`/inherited-ACL behavior?
10. What is the durability reality of network filesystems (SMB/NFS) and of WSL2's `/mnt/c` vs the ext4 VM disk — which Semantics silently weaken `fsync`, atomic rename or locking there?

### Signals, time and events

11. Signals (`SIGTERM`/`SIGINT`/`SIGWINCH`/`SIGHUP`) vs Windows console control events (`CTRL_C_EVENT`, `CTRL_CLOSE_EVENT`) and `TerminateProcess`: what can and cannot be caught, ordered or flushed, and how does a detached service avoid console-attachment coupling?
12. Event loops and readiness: `epoll` vs `kqueue` vs IOCP completion vs `io_uring`, file-descriptor limits/`select` ceilings, and Unix-domain sockets vs Windows named pipes — which give the broker a single cancellation/backpressure story?
13. Time and monotonicity: monotonic vs wall clock, NTP slew/step and suspend/resume, timezone/DST handling, and which time source may enter canonical state (recall: logical time enters only as an admitted input per `E-FHAR2-102`)?

### Resource limits and service models

14. Resource limiting: Linux `rlimits`, `cgroup v2` (`memory.max`, `cpu.max`, `io.max`, OOM kill behavior, `memory.events`) vs Windows Job Object limits (CPU rate, commit, process count, memory) — how does a limit become a *fault* versus a throttle, and how is OOM observed?
15. Service supervision: systemd unit design (Type, restart policy, watchdog `WATCHDOG=`, socket activation, `RuntimeDirectory`, `LoadCredential`, hardening directives) vs the Windows Service Control Manager (recovery actions, failure counters) — how does each avoid silently restarting killed work or racing PAUSE?
16. Crash and recovery semantics per OS: what survives a hard kill, a reboot, a power loss, and a suspended laptop; how are PID reuse, stale lock files, and half-written state detected and reconciled?

### Locale, security and conformance matrix

17. Locale, encoding and environment: `LANG`/`LC_*`, Windows code pages and UTF-8 manifest, `TZ`, temp dirs and noexec/tmpfs, environment inheritance into workers, and secret leakage via environment or command line?
18. Security modules and platform hardening: SELinux/AppArmor on Linux and Defender/ASR/Controlled Folder Access on Windows — what breaks silently under each (rename, exec, socket, filesystem access), and how is a denial surfaced rather than swallowed?
19. What is the **per-OS conformance test matrix** required to graduate a platform, and how are Git Bash/MSYS2, WSL2 (both `/mnt` and native ext4), and Neovim embedded terminals treated as distinct high-priority cases rather than aliases of native Windows or Linux?

## Deliverable

A source-pinned report containing: a claim table (claim, source, version/date, classification, confidence, expiry trigger); a per-OS conformance matrix whose every cell is measured, explicitly modelled, or `unknown`; a preregistered cross-platform fault-test plan (descendant escape, PTY teardown, atomic-rename crash, stale lock, disk-full, OOM, suspend/resume); a recommendation tied to a named competing option and decisive falsifier; and open Owner questions. No implementation is selected as a product default.

## Non-goals

- No OS-specific runtime or broker implementation.
- No selection of a process library, PTY library, renderer or multiplexer.
- No OS terminal application, distro or kernel version made mandatory.
- No reopening of the detachable-terminal or containment product rulings.
- No claim of full descendant-tree termination or durability before it is measured across the matrix.
- No graphical frontend or Windows installer work.
- No provider, model or route calls.
- No modification of `docs/v3/TERMINAL-RUNTIME.md`, `docs/v3/STATE.md` or any protected path.

## Completion gate

- Primary platform sources are pinned by version/date (Microsoft pseudoconsole, Job Objects, `ReplaceFile`/`FlushFileBuffers`, Windows console events; Linux `renameat2`, `cgroup v2`, `clone3`, PTY/termios, `rename(2)` and `fsync` semantics; POSIX.1-2024 where used).
- Every containment claim names a specified negative trace (escape, orphan, reparent, stale lock).
- The per-OS conformance matrix is preregistered with explicit stop conditions.
- Native Windows and Linux are mandatory; Git Bash/MSYS2, WSL2 (both mount paths) and Neovim embedded terminals are distinct cases.
- Filesystem durability, locking and `MAX_PATH`/long-path behavior are covered for each OS and for SMB/NFS.
- Every recommendation names a competing option and a falsifier, and preserves an explicit no-selection outcome.
- Protected paths are untouched; privacy and secret scan are clean.
