# Same-uid cgroup v2 leaf launch (CSL-728)

`DelegatedRoot(path).launch(name, argv)` starts `argv` in a new leaf
`<root>/run-<id>` and returns a `LeafRun`. Use it as a context manager: every exit
path (normal return, exception, launch failure, cancellation) kills the leaf.

## How it works

- The root must be an absolute, symlink-free directory below the cgroup v2 mount,
  owned by this uid together with its `cgroup.procs` (systemd delegation, for
  example a unit with `Delegate=yes`), with `cgroup.kill` present (Linux 5.14+).
  The launching process must itself be inside that subtree, because cgroup v2
  only lets an unprivileged writer migrate a process when it can write the
  common ancestor's `cgroup.procs`. Leaf names match `run-[A-Za-z0-9_-]{1,64}`;
  an existing leaf is never reused.
- No launch race: the parent opens the leaf's `cgroup.procs` and starts a small
  trampoline that writes its own pid there, then execs the target. If the write
  or exec fails, it reports through a close-on-exec pipe and exits without
  running the target. The parent then checks `/proc/<pid>/cgroup` names the leaf.
  No `preexec_fn`, no parent-death signal, no moving a running child.
- Identity: the parent opens a pidfd for the unreaped child right after spawn and
  signals through it, so a reused pid is never signalled.
- Kill: write `1` to `cgroup.kill`, SIGKILL the main process through its pidfd,
  and repeat until the deadline. Reads and writes of `cgroup.kill` and
  `cgroup.events` stop at that deadline, so a stuck cgroup file or a process
  that is slow to exit cannot hold cleanup open. The outcome is `leaf_empty`
  only when `cgroup.events` reads `populated 0` and the main process's exit was
  seen by `waitpid`. Otherwise it is `running` (still populated) or `unknown`. This
  launcher never books `terminated`: a same-uid escapee is unseen (see Limits),
  so an empty leaf does not prove the tree ended (PRODUCT.md 055cc7a5 §3.4 S5 and
  the Stop bullet, C1-27). Every outcome carries `escape_observable=False`;
  `terminated` is reserved for a launch path that can observe escapes. A populated
  or unobserved leaf is never removed: populated `0` with the main exit unseen is
  `unknown`, and the leaf and pidfd are kept so a later `close()` retries the kill.
  The directory is removed only after `cgroup.procs` and `cgroup.threads` (if
  present) list no ids and it has no child cgroups and no symlinks; `rmdir` is
  not called before that. `EBUSY` while the directory is not empty stops
  immediately. `EBUSY` while it still reads empty (a dying task still holds the
  cgroup) is retried until the same deadline, then the directory is left in
  place. The handle remembers the directory's device and inode. A later
  `close()` kills or removes only that directory. If the identity was never
  known, or a different directory is now at the path, the result stays
  `unknown` and that directory is left in place.
- Restart: `sweep(keep)` kills leftover leaves the durable run registry no longer
  lists and books them `empty`, not `terminated`, because their exits were not
  seen. `keep` is required (no kill-everything default) and must name every leaf
  the registry still owns, including another launcher's on the same root; those
  leaves are untouched. Names that are not valid leaf ids are still swept when
  they start with the recursion-owned prefix `run-`. Names outside that prefix,
  and symlinks, are left alone. `adopt(name)` returns a handle for a kept leaf so the
  restarted daemon can kill it later; its best outcome is `empty`
  (PRODUCT.md 055cc7a5 §3.1, Q38/O53).
- Errors: a cleanup problem never replaces the original exception; it is added
  as a note. On a clean exit, incomplete cleanup raises `CleanupIncomplete`.
- Interrupted cleanup: `finally` can be interrupted (PRODUCT.md 055cc7a5 §3.4,
  C2-10), so an exception raised inside cleanup (a second `KeyboardInterrupt`, for example)
  is noted and the kill retried, up to 5 attempts, on every exit path; the
  interrupt still propagates. Only a final outcome (leaf removed) is cached; any
  other `close()` outcome is retried by the next `close()`. If every attempt is
  interrupted the leaf stays on disk, and the startup `sweep()` is the backstop.

## Limits

- Same uid only; there is no cross-uid kill path here. Same uid cannot hold a
  process in the leaf against a user-manager escape. The child can run
  `systemd-run --user`, or write any other `cgroup.procs` it owns, and leave the
  leaf. The escaped process is unseen, and the leaf can read empty while it lives.
  This is not a containment or security boundary against a same-uid process.
- Cross-uid (B6 proper) comes later and must require that the engine user has no
  user manager during runs: no linger and no running `user@<engine uid>.service`.
  Engine linger plus an operator-owned leaf is not containment. Lucas, 2026-11-07,
  recorded on B6 and B9: "linger for the engine user and leaf containment
  conflict ... design the cross-uid path so the engine has no user manager during
  runs." Owner-reported host evidence (gs-worker-nbg, Ubuntu 26.04.1, kernel 7.0,
  systemd 259, 2026-11-07; recorded on CSL-728 and CSL-738, not re-measured here): a
  lingering engine user inside a root-created wrapper unit ran
  `systemd-run --user` (exit 0) and started a process in its own
  `user@.service/app.slice` tree, outside the wrapper. It was still the same uid,
  so this was not privilege escalation.
- Tests use a fake cgroupfs that models a leaf as a process group, with real
  processes. They exercise the trampoline, pidfd and evidence logic, not kernel
  cgroup behaviour. The live cell runs only when `RECURSION_CGROUP_ROOT` points at
  a writable delegated root. The hd18/escgrp/dwdump cells are not run here.
- Open for Lucas (CSL-738 wiring): the launcher must sit in the delegated
  subtree, so if the daemon unit is the launcher, its runs sit inside the
  daemon's own unit and a default `KillMode=control-group` stop ends them,
  against §3.1 ("runs live outside the daemon's own unit"). Which process
  launches, and where the delegated root sits, is not decided here.
- Child environment (B1) is not handled here: `env` is passed through as given.

## B6 evidence contract (Lucas, 2026-11-08)

Lucas's 2026-11-08 call: the engine user gets no linger and no systemd user manager;
the operator keeps linger. `b6_evidence.evaluate(record)` judges a recorded host
run against every B6 bar. It runs no probe, touches no systemd or cgroup, and
switches no uid.

- Kept bars: escapers 0/120; engine `cgroup.procs` writes refused 10/10;
  `systemd-run --user` refused or inside the leaf 10/10; `terminated` never booked
  while `populated` is 1.
- Migration probe, n=10 per method, engine user manager absent (`user@` inactive,
  `systemd/private` absent): a process already in the leaf, as the engine uid,
  tries `systemd-run --user --scope -- sleep 60`, a direct write of its own pid
  to a `cgroup.procs` outside the leaf, and an unprivileged
  `AttachProcessesToUnit` call for its own pid on the system bus (O65: PID 1
  checks only process ownership; the attempt records bus, call, unit, subcgroup
  and pids, and must be refused). Each attempt runs from its own caller
  process: a caller pid repeated within an arm fails, so one trial copied ten
  times is not 10/10. Each attempt records the caller's uid
  (must be the record's `engine_uid`, not the operator's), its pid, its cgroup
  before the attempt (must already be in the leaf) and the run's expected pids.
  A manager-absent scope attempt is a refusal only when `exit_code` is not 0,
  `sleep_cgroup` is `never_started`, `refused` is true, and `error` is a
  non-empty string. `systemd-run --scope` returns the command's own status, so
  exit 124 or 143 with a missing `sleep_cgroup` is not a refusal. A cgroup path,
  including one inside the leaf, is not a refusal either. A direct write must
  name a canonical `cgroup.procs` outside the leaf, write the caller's own pid,
  and record `refused: true`. An attach attempt must name `AttachProcessesToUnit` on the
  system bus for the caller's own pid and record `refused: true`. Afterwards
  `/proc/<pid>/cgroup` must name the leaf, or a cgroup below it, for exactly the
  expected pids. Paths must be absolute and canonical (no `.`, `..`, `//`).
- Control, engine user manager present, all three methods n=10: all 10 scope
  attempts must exit 0 with the sleep in a canonical cgroup outside the leaf,
  reproducing the 2026-11-07 escape; the 10 direct writes and 10 attach calls need a
  recorded outcome either way.
  Otherwise the verdict is `invalid`: the probe is not reaching the user manager,
  and no pass is read. Malformed records fail closed and never raise.
- B33: `spawn_order` must show `PR_SET_PDEATHSIG` armed after the uid switch and
  before exec (the kernel clears it on a credential change), from one long-lived
  spawning thread (B33): `pdeathsig_spawner` records the thread's `tid`,
  `long_lived: true`, and `spawn_tids`, every one equal to `tid`. The signal
  fires when the forking thread exits, so a missing, short-lived or second
  spawning thread fails. This same-uid
  launcher still never arms it, and cleanup never relies on it (CSL-706).
- Verdicts: `pass` needs `evidence_class: host` plus host, kernel, systemd,
  capture time and the command log's SHA-256 (64 lowercase hex characters; the
  contract reads no files, so matching it to the log is the reviewer's check).
  Any other class is `structural` at
  best. The tests use hand-built fixtures, so they are structural only; no host
  evidence for these bars exists yet.
