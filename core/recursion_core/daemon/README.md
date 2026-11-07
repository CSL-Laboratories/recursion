# Linux user daemon (CSL-738)

One daemon per user on each machine, a durable run registry and a startup sweep.
Linux only; no Windows (CSL-746) and no cross-uid code.

## Install (operator)

Install `core/` to `~/.local/lib/recursion/core` and a free-threaded CPython 3.14t
venv to `~/.local/lib/recursion/venv`. Copy `core/systemd/recursion-daemon.service`
to `~/.config/systemd/user/`, then `systemctl --user daemon-reload` and enable the
service. The daemon refuses to start on a GIL build. Plugin loading must call
`require_free_threaded()` after each import, before invoking plugin code.

Host prerequisites (founder setup, not performed by this package): cgroup v2,
`user@.service` delegation, and `loginctl show-user USER -p Linger` returning
`Linger=yes` for the operator and, when `RECURSION_ENGINE_USER` names a user that
exists on the host, the engine user. `check_linger` only reads; nothing here runs
`loginctl enable-linger`. The daemon fails closed when Linger is not `yes`.

## Behaviour

- Singleton: `flock` on `$XDG_RUNTIME_DIR/recursion-daemon.lock`. The kernel drops
  the lock when the process dies, so a stale file never blocks a restart. The file
  is never unlinked. Different users have different runtime directories.
- Registry: SQLite with `synchronous=FULL` in `$XDG_STATE_HOME/recursion/`
  (directory 0700, file 0600). A run row commits before `systemd-run`; a Stop
  intent commits before `systemctl stop`. Unknown schema versions are refused.
- Each run is its own transient `recursion-run-<id>.service` user unit with its own
  cgroup, not part of the daemon unit. Closing, restarting or `kill -9` of the daemon
  does not stop runs; the sweep re-adopts them. The `recursion-run-` namespace is
  reserved: the sweep stops units there that have no live registry entry.
- States: `starting`, `running`, `stopping`, `unknown`, and the sticky terminal
  states `terminated` (cgroup positively observed empty, `populated 0`, in this
  boot) and `lost_reboot`. A run with a recorded Stop is never reported `running`.
  Anything not positively observed is `unknown`, never `terminated`.

## Reboot

The registry survives a reboot; processes and transient units do not. Each run
records the kernel `boot_id`. After a reboot the sweep marks earlier-boot runs
`lost_reboot`: not `terminated`, because nothing saw them end, and not relaunched,
because relaunching a provider workload needs a founder policy and the R2a gate.
Workload checkpoint and restart after reboot is not implemented.

## Limits

- If the systemd user manager itself stops (no Linger at last logout, or
  `user@.service` restart), it stops the run units too. Linger is the guard.
- Fake-manager tests only. Live restart, `kill -9`, supervisor death, reboot,
  last-SSH-session and the 8 lifecycle cases on a real host remain founder work.
- No control API, provider launch route or token file here; this lifts no R2a,
  account or spend gate. The same-uid beta is not a security boundary against a
  hostile process of that uid.
