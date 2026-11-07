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
  and repeat until the deadline. The outcome is `terminated` only when
  `cgroup.events` reads `populated 0` and the main process's exit was seen by
  `waitpid`. Otherwise it is `running` (still populated) or `unknown`. A populated
  or unobserved leaf is never removed. `sweep()` kills leftover leaves after a
  launcher crash and books them `empty`, not `terminated`, because their exits
  were not seen.
- Errors: a cleanup problem never replaces the original exception; it is added
  as a note. On a clean exit, incomplete cleanup raises `CleanupIncomplete`.

## Limits

- Same uid only; there is no cross-uid kill path here. Same uid cannot hold a
  process in the leaf against a user-manager escape. The child can run
  `systemd-run --user`, or write any other `cgroup.procs` it owns, and leave the
  leaf. The escaped process is unseen, and the leaf can read empty while it lives.
  This is not a containment or security boundary against a same-uid process.
- Cross-uid (B6 proper) comes later and must require that the engine user has no
  user manager during runs: no linger and no running `user@<engine uid>.service`.
  Engine linger plus an operator-owned leaf is not containment. Lucas, Nov 7,
  recorded on B6 and B9: "linger for the engine user and leaf containment
  conflict ... design the cross-uid path so the engine has no user manager during
  runs." Owner-reported host evidence (gs-worker-nbg, Ubuntu 26.04.1, kernel 7.0,
  systemd 259, Nov 7; recorded on CSL-728 and CSL-738, not re-measured here): a
  lingering engine user inside a root-created wrapper unit ran
  `systemd-run --user` (exit 0) and started a process in its own
  `user@.service/app.slice` tree, outside the wrapper. It was still the same uid,
  so this was not privilege escalation.
- Tests use a fake cgroupfs that models a leaf as a process group, with real
  processes. They exercise the trampoline, pidfd and evidence logic, not kernel
  cgroup behaviour. The live cell runs only when `RECURSION_CGROUP_ROOT` points at
  a writable delegated root. The hd18/escgrp/dwdump cells are not run here.
- Child environment (B1) is not handled here: `env` is passed through as given.
