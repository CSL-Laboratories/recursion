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

- Same uid only. A process of the same uid can move itself out of the leaf, so
  this is not a security boundary against a hostile same-uid process. An escaped
  process is unseen; the leaf can read empty while it lives. Cross-uid (B6 proper,
  engine under a second uid) comes later.
- Tests use a fake cgroupfs that models a leaf as a process group, with real
  processes. They exercise the trampoline, pidfd and evidence logic, not kernel
  cgroup behaviour. The live cell runs only when `RECURSION_CGROUP_ROOT` points at
  a writable delegated root. The hd18/escgrp/dwdump cells are not run here.
- Child environment (B1) is not handled here: `env` is passed through as given.
