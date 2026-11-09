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
`user@.service` delegation, and `Linger=yes` for the **operator** (B9), so the
operator's user manager, this daemon and its run units outlive the last SSH
session. `check_operator_linger` only reads `loginctl show-user`; nothing here
enables or disables lingering or touches any service. The daemon fails closed
when operator Linger is not `yes`.

Operator linger is not engine linger. Lucas (Nov 7, recorded on B6 and B9):
linger for the engine user conflicts with leaf containment. Owner-reported host
evidence (gs-worker-nbg, Nov 7, recorded on CSL-728 and CSL-738): a lingering
engine user inside a root-created wrapper unit ran `systemd-run --user` and
started a process in its own `user@.service` tree, outside the wrapper. Nov 11 is
same-uid only, so the daemon refuses to start when `RECURSION_ENGINE_USER` is set.
The later cross-uid path must require that the engine user has no user manager
during runs: no linger and no running `user@<engine uid>.service`. So the engine
user does not get its own lingering `systemd --user` daemon; Q38's "engine user
gets its own daemon" clause stays open under that invariant.

## Engine user: no linger, no user manager (Lucas, Nov 8)

Lucas's Nov 8 call replaces "Linger=yes for both": the operator keeps
`Linger=yes`; the engine user gets `Linger=no` and no `user@<engine uid>.service`.
Stop still ends just the one run. `engine_user.py` holds read-only pieces for the
later cross-uid path, which this package still refuses. The daemon does not call
them, so it does not enforce engine-manager absence today; same-uid operator runs
are unaffected:

- `check_engine_no_user_manager(user, uid)` passes only when `loginctl show-user
  <engine> --property=Linger --value` says `no` (or logind says the user is not
  logged in or lingering), `systemctl is-active user@<uid>.service` says
  `inactive`, and `/run/user/<uid>/systemd/private` does not exist. Anything else,
  including a probe that cannot run, fails closed. It enables, disables, masks or
  stops nothing. `systemctl mask user@<uid>.service` is proposed hardening,
  unmeasured, and not part of this bar.
- `refuse_session_launch(argv)`: a guard for the later cross-uid engine spawn. It
  refuses launchers that open a PAM/logind session and would start the engine
  user's manager again (`su`, `runuser`, `sudo`, `login`, `machinectl`, `pkexec`,
  `doas`, `ssh`, `run0`, and `systemd-run` with `PAMName=`). It is not applied to
  same-uid operator runs.
- `SPAWN_PATH` records the spawn path this package uses today: same-uid transient
  `systemd-run --user` units in the operator's user manager, no engine uid, no PAM.
  B7 (CSL-734) picks the cross-uid spawn path; nothing here chooses it.
- `signin_without_session_bus(engine_env, uid)` is an offline check that the
  engine env names no session bus and no bus socket exists for the engine. It never
  opens a credential store, and its verdict is at most `pending`. Store paths are
  listed only where repository evidence names them: Codex `~/.codex/auth.json`
  (cited by #321 `subscription_routes/policy.py`, CSL-712 H3); Claude Code is
  `unconfirmed`. Behaviour with the bus absent is unconfirmed for both until a
  founder-attended signed-in run of each.

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
  states `terminated` (systemd saw the run unit end in this boot: its cgroup read
  `populated 0`; or the loaded unit is `inactive`, `failed` or `active (exited)` with
  its cgroup already pruned; or a synchronous `systemctl stop` returned and the unit
  was then garbage-collected; or, for a run with a recorded Stop, `systemctl stop`
  answered "Unit ... not loaded" (exit 5) and the unit reads not-found, as when a
  replayed Stop finds the unit an earlier stop already ended and collected; the
  orphan sweep stops any same-name unit that appears later) and `lost_reboot`. A run with a recorded Stop is never reported `running`.
  Anything not positively observed is `unknown`, never `terminated`.

## Reboot

The registry survives a reboot; processes and transient units do not. Each run
records the kernel `boot_id`. After a reboot the sweep marks earlier-boot runs
`lost_reboot`: not `terminated`, because nothing saw them end, and not relaunched,
because relaunching a provider workload needs a founder policy and the R2a gate.
Workload checkpoint and restart after reboot is not implemented.

## Limits

- Same uid: a run can call `systemd-run --user`, or write another `cgroup.procs`
  it owns, and leave its unit's cgroup. The escaped process is unseen, and the
  unit can read empty while it lives. `terminated` means systemd saw the run unit's
  cgroup empty or the unit end, not that every process the run started is gone. Same uid is not a
  containment or security boundary.
- If the systemd user manager itself stops (no Linger at last logout, or
  `user@.service` restart), it stops the run units too. Linger is the guard.
- Fake-manager tests only. Live restart, `kill -9`, supervisor death, reboot,
  last-SSH-session and the 8 lifecycle cases on a real host remain founder work.
- No control API, provider launch route or token file here; this lifts no R2a,
  account or spend gate. The same-uid beta is not a security boundary against a
  hostile process of that uid.

## B14: a recorded Stop survives a daemon crash (kill -9 cells)

`Daemon.stop` commits the Stop intent (`stop=1`, state `stopping`) before any signal.
`Registry.state` never writes `running` for a run with a recorded Stop, even from a
caller holding a `Run` read before the Stop committed; a live unit reads `stopping`.
The startup sweep replays the Stop and records `terminated` only after the run is
positively gone.

`tests/test_stop_crash.py` measures this with real processes: a real daemon process
(`tests/crash_child.py`) starts a real run in its own session and is killed with
SIGKILL, n=10 per phase:

- B8 `running` (no Stop): the run outlives the daemon, the kernel releases the
  daemon lock, and a new daemon re-adopts the same pid as `running`.
- B14 `before_signal`: killed after the Stop intent committed and before any
  signal. A first restart sweep whose Stop has not reached the run yet reads
  `stopping` with the run alive; the next sweep replays the Stop and reaches
  `terminated`.
- B14 `after_signal`: killed after the manager's stop returned (SIGTERM sent,
  grace wait and any SIGKILL done) and before the daemon recorded the outcome. The
  test manager's stop waits (up to 5 s) after a SIGKILL escalation until the run is
  dead or a zombie, so the cell's "run gone" check cannot race the kill
  (`StopEscalationWaits`, n=10). The run is already gone at the kill, so this
  measures the record step only. The restart sweep reads `terminated`; never
  `running`.
- B14 `signal_sent`: the run records and ignores SIGTERM; killed right after
  SIGTERM reached it, while the signalled run is still alive. A first restart sweep
  whose Stop has not reached the run yet reads `stopping` with the run alive; the
  next sweep replays the Stop and escalates to SIGKILL after a 1 s grace. It still
  reads `stopping` when the kill has not landed by its observation; a later sweep
  reaches `terminated` once the run is positively gone.

The service manager in these cells is the test-only `tests/procmanager.py`
(one session and process group per run, pid plus kernel start time per record),
not systemd: the measuring box has no systemd PID 1 or user manager. Not
measured here: systemd `--user` scopes and `systemctl --user restart`, a real
reboot, operator linger on a host, and Windows (job handle held outside the
daemon, CSL-746).

## Loopback GUI (O68 bar 4 / A45, Nov 8 correction)

The service composition root now starts `LoopbackGUI` beside reconciliation and
closes its HTTP listener before closing the registry. The listener binds only
`127.0.0.1:8765` (`--gui-port` overrides the port, never the address). The GUI package
opens no socket. If that port is already taken (any `OSError` on bind), the
daemon logs "GUI unavailable" once, keeps its five-second reconciliation and Stop
replay running, and retries the bind on every sweep; it logs "GUI available again"
when the bind succeeds. A bad plugin registry still stops the service, and it is checked before any bind, so it fails at startup even while the port is occupied. Each plugin's panels and required subjects are read once into a snapshot that both the startup check and the served page use, so generator-backed registries are not emptied by the check. This branch carries the #330 GUI prerequisite unchanged by
cherry-pick so its real HTTP test is self-contained; merge order is not assumed.

The control-API device door and `ControlClient` can be injected into `main`.
Without the #327 adapter the production default refuses authentication on every
request (401); it does not silently trust loopback, invent data, or manufacture a
token. Tests inject a fake API/device door and prove a real GET returns the page.
Control-API credential/pairing integration remains with #327, not claimed here.

For an installed Cockpit plugin, configure the composition root with the explicit
trusted factory `--plugin cockpit.gui.panels:registry` (Cockpit's `engine` must be on the
module path). The root passes the registry's plain `required`/`panels` mappings to
`recursion_core.gui.compose`; Cockpit does not import Recursion. `/panel?id=...`
renders only the named O7 reads, with the shared venue-data filter, no panel order
verb and no panel Stop. Shared interventions and run Stop stay in the core page.

Contract: `contracts/PRODUCT.md` at
`055cc7a5c793b7135e5d0565dc4198cf96230531`, SHA-256
`e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf`.
