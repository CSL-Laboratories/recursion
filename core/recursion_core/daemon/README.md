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
  (directory 0700, file 0600). Those modes are set with `chmod` / `fchmod`, so a
  process umask of 000 cannot leave the directory world-writable or a registry
  file group- or world-accessible. An existing directory owned by this user is
  tightened to 0700; a directory owned by someone else is refused. Each committed
  change is written to a temp file in that directory, fsynced, and renamed over
  `runs.sqlite3`. Before that rename the live database is checkpointed and its
  `-wal`, `-shm`, and `-journal` files are removed, so a crash after the rename
  cannot replay a previous WAL onto the new file, and a crash before the rename
  leaves the previous file with its committed rows in it. The directory fsync
  is the barrier after that rename and before the file is reopened. If it
  fails, the published file stays and the call raises `RegistryDurabilityError`:
  the rename may be visible without being crash-durable, so a read, a sweep, a
  start, or a stop does not run until a later directory fsync succeeds. If the new file cannot be reopened, later calls raise
  `RegistryIntegrityError` until an open succeeds. Temp files left by a dead
  pid are removed on open; a live pid's temp file is left in place. A zero-length
  or truncated registry is refused; it is not opened as an empty registry. A run
  row commits before `systemd-run`; a Stop intent commits before `systemctl stop`.
  Unknown schema versions are refused.
- Each run is its own transient `recursion-run-<id>.service` user unit with its own
  cgroup, not part of the daemon unit. Closing, restarting or `kill -9` of the daemon
  does not stop runs; the sweep re-adopts them. The `recursion-run-` namespace is
  reserved: the sweep stops units there that have no live registry entry.
- States: `starting`, `running`, `stopping`, `unknown`, and the sticky terminal
  states `terminated` (the run's process is attested gone in this boot: its cgroup
  subtree read `populated 0` and no pid in any descendant `cgroup.procs` or `MainPID` was still alive; or
  the loaded unit is `inactive`, `failed` or `active (exited)` with its cgroup
  already pruned and its main pid gone; or a synchronous `systemctl stop` returned
  and the unit was then garbage-collected; or, for a run with a recorded Stop,
  `systemctl stop` answered "Unit ... not loaded" (exit 5) and the unit reads
  not-found, as when a replayed Stop finds the unit an earlier stop already ended
  and collected; the orphan sweep stops any same-name unit that appears later) and
  `lost_reboot`. A unit systemd no longer knows is never ended on systemd's word
  alone: when the run's control record carries a cgroup path, that path is walked,
  what is still alive there is killed through the cgroup (`cgroup.kill`, else
  SIGKILL by pidfd to each listed pid /proc still places there), and only an
  absent or empty cgroup is `terminated`; otherwise the run stays `stopping`. A Stop
  brings the cgroup path its receipt will name, and that path is walked the same way
  when the control record carries none (a launch whose record was never written).
  With no path at all the row ends on systemd's word only for a run that has no
  control record; a run whose control record exists and carries no cgroup stays
  `unknown` (`unit gone; the control record carries no cgroup to walk`) through every
  sweep and fence, until a Stop brings the path or the record gains it. A launch
  that never started a unit waits there too: nothing durable tells the two apart yet.
  `run.stop` answers `stopped` only after the path it names read absent or empty in
  the same call, whatever the row says. A scripted observation that the unit ended, or a listener that is
  still alive, is not `terminated`. A read of `cgroup.procs` or `/proc/<pid>/stat`
  that fails with anything other than ENOENT or ESRCH is unknown, not an empty
  cgroup and not a dead process, and is not `terminated`. A run with a recorded
  Stop is never reported `running`. Anything not positively observed is
  `unknown`, never `terminated`.

A stop request that failed (`systemctl` hung, exited oddly or is missing) is sent
again by the sweep only after a wait that doubles from 30 s to 10 min; a Stop the
operator asks for is always sent. An observation that changes nothing is not
written, so an unconfirmable Stop does not rewrite the registry on every sweep.

## Reboot

The registry survives a reboot; processes and transient units do not. Each run
records the kernel `boot_id`. After a reboot the sweep marks earlier-boot runs
`lost_reboot`: not `terminated`, because nothing saw them end, and not relaunched,
because relaunching a provider workload needs a founder policy and the R2a gate.
Workload checkpoint and restart after reboot is not implemented.

## Limits

- Same uid: a run can call `systemd-run --user`, or write another `cgroup.procs`
  it owns, and leave its unit's cgroup subtree. The escaped process is unseen.
  A process that is still in a descendant cgroup, including one the run created
  because the unit has `Delegate=yes`, is inside the subtree: it is not that gap,
  and it blocks `terminated`. An unfinished walk of the subtree is `unknown`,
  including one that hits a depth limit, a node limit, or a cycle. The node
  limit is charged for each directory entry before that entry is read or
  stat'd, and for each child still queued. The listing is `os.scandir`, closed
  once the budget is spent, so a wide directory is not read in full.
  `terminated` means the subtree was empty or the unit ended, not that every
  process the run started is gone. Same uid is not a containment or security boundary.
- If the systemd user manager itself stops (no Linger at last logout, or
  `user@.service` restart), it stops the run units too. Linger is the guard.
- Fake-manager tests only. Live restart, `kill -9`, supervisor death, reboot,
  last-SSH-session and the 8 lifecycle cases on a real host remain founder work.
- The control API and the launch route are composed in (see "Composition" below);
  this lifts no R2a, account or spend gate. The same-uid beta is not a security boundary against a
  hostile process of that uid.

## Composition: control API, launcher and surfaces

Spec: PRODUCT.md d84cdd26 (CSLR-D-012), section 3.1 (lines 457-469: one daemon per user, runs
survive the daemon, runs live outside its unit), section 3.4 S6 (line 888) and section 11.3
(lines 3039-3041: the #916 procedure).

`python3.14t -X gil=0 -m recursion_core.daemon` composes, after its startup sweep:

- **Control API on a Unix socket.** `control_adapter.compose_control` builds
  `control.api.ControlAPI` and serves it with `control_transport.UnixControlServer` at
  `$XDG_RUNTIME_DIR/recursion/control.sock` (directory 0700, socket 0600, peer uid checked
  on both ends). A peer whose cgroup is a `recursion-run-*` unit, or cannot be read, is
  refused (`scope_refused`): the uid check alone would let an engine child stop or start
  runs. The refusal is decided at accept, before the peer takes a connection slot, and
  again on every request. When all 16 slots are held, four reserved connections each
  serve one `run.stop`; anything else there is `busy`, not delivered. A process that
  left its unit's cgroup passes this check (see Limits). Finer scopes do not exist yet.
- **CLI.** `python -m recursion_core.control list|status|stop|start|conditions` talks to that
  socket. Daemon down: `{"code": "control_api_unavailable", "delivered": false}`, exit 1.
- **`run.list` fits the frame (ADV1-5).** Spec: PRODUCT.md d84cdd26 (CSLR-D-012), section 3.6.
  Control records are never pruned, and at about 975 bytes per listed run a full list passed the
  1 MiB frame near 1075 runs (`daemon:response_too_large`). `DaemonControl` now observes the runs of
  the page the API answered (all runs, or `limit` / `after`) one at a time and stops before the reply
  would pass `LIST_REPLY_BYTES` (the frame less 4096 bytes of envelope) less the size of the
  request's echoed `id`. A cut page answers `ok` with `next_after` naming the last run sent. A list
  that fits is returned unchanged. A single run too large for a frame on its own is still sent, and
  the transport answers `response_too_large` as before; it is never skipped. The CLI's `list` and
  `conditions` and the TUI follow `next_after` to the last page (`tui.protocol.list_all_runs`); the
  GUI has no run list. Nothing is pruned or deleted, and the frame limit is unchanged. The other
  readers of every run (`reconcile`, the breaker-store check at start) read the registry in process
  and never go through a frame. Params, codes and the follow rules: `control/README.md`.
- **Durable run records.** `DurableRunRegistry` keeps the control API's records
  (compare-and-swap, attested Stop, start keys) in `runs.sqlite3`, table `control_runs`,
  written with the registry's own fsync, rename and directory-fsync steps.
- **Launcher.** `launcher.ProductionLauncher` is the launcher the Claude Code and Codex
  route adapters call. One run is one `recursion-run-<run id>.service` transient user unit
  (`engine_user.SPAWN_PATH`). Its environment is what `child_env.build_child_env` accepts
  from the adapter's environment (route-owned HOME under
  `$XDG_STATE_HOME/recursion/homes/<engine>`); it reaches the engine through a FIFO and
  `engine_exec.py`, never through argv or a unit property. stdout and stderr are files in
  `$XDG_STATE_HOME/recursion/runs/<run id>/`, so a dead daemon never gives the engine a
  broken pipe; stdin is a FIFO.
- **Stop.** Units carry `KillSignal=SIGINT`, `TimeoutStopSec=2s`, `KillMode=control-group`,
  so `systemctl --user stop` is the #916 procedure. `run.stop` records the Stop intent,
  stops the unit and answers `stopped` only after the cgroup path it names read absent or
  empty in the same call (see "Behaviour"); anything else is `pending` with a reason. A
  deadline or protocol error fences the run the same way.
- **Restart.** After the startup sweep, `DaemonControl.reconcile` finishes any Stop the
  registry recorded but the control record did not, records the cgroup of a launch the
  previous daemon did not finish recording, and marks a start that never reached a unit
  `start_failed`. It runs again after every sweep.
- **What a surface reads.** `run.status` and `run.list` add `observed` (the unit's last
  observed state). A record that says `running` while the unit was attested ended reads
  `state: "ended"` (`"lost_reboot"` after a reboot, `"unknown"` when the unit could not be
  observed) with the stored word in `recorded_state`. `run.status` also carries the tail
  of the engine's stdout (`output`) and the route result (`result`).
- **GUI.** The loopback GUI gets the same object as its client and a real door:
  `DeviceDoor` accepts only `Authorization: Bearer <token>` or the `recursion_device`
  cookie matching a per-start random token in
  `$XDG_RUNTIME_DIR/recursion/gui-device-token` (0600), written once the control socket
  is this daemon's. An existing runtime directory that is not 0700 is refused, not
  repaired. Each connection has its own thread (at most 16) and five seconds for its
  whole header. Its Stop is `run.stop`. Its route
  table is the daemon's own inventory (`route.list`): one row per engine the daemon has an
  adapter for (configured or `route_not_configured`), the CLI surface, the plugin panels
  and one `plugin:<id>` row per installed plugin, loaded or refused. The daemon records no
  bar evidence, so every row is BETA. Its O7 reads go to the loaded operator plugin's
  `operator(command)`. A read with no source answers `unavailable: <reason>` (no plugin
  installed, no loaded plugin serves the contract, the plugin failed, verification
  unavailable); the O7 interventions are not wired and answer `unavailable` too.
- **TUI.** `python -m recursion_core.control.tui [--plain]` is the TUI on the same socket
  client as the CLI, so its `start`, `list`, `status` and `stop` are the daemon's `run.*`
  calls. Daemon down: the TUI shows `control_api_unavailable` (`UNAVAILABLE`), no traceback.
- **Plugins.** Loaded only through `plugin_security.PluginLoader` (`plugins.PluginHost`):
  manifest, artifact digest, signature against this install's trusted keys and the
  installer's grant are checked before any plugin code runs. A refused plugin is listed
  with its typed finding (`plugin.list`, and the GUI's route table) and the daemon runs
  without it. With no `cryptography` every plugin is refused
  `plugin_verification_unavailable`; none loads unverified. Layout, the install file and
  the interpreter recipe are in `plugin_security/README.md`. Plugins run in the daemon's
  process and uid: this is not a containment boundary.
- **Breaker.** The route breaker writes typed connection limits, each run's binding
  (connection, engine, model, owned by `host`) and faulted runs to
  `$XDG_STATE_HOME/recursion/plugin-security/breaker.sqlite3` through
  `plugin_security.ConnectionBreaker` (`breaker.DurableRouteBreaker`). After a restart a
  connection the store calls tripped is fenced again at its first request for the breaker's
  cooldown (the vendor's reset time is not stored), then one leased probe is admitted and
  only its success clears the trip. A model-bucket limit stays in memory. A store that
  exists and cannot be read refuses every start (`breaker_store_unavailable`); reads and
  Stop still work. Before a unit starts, its admitted connection, engine and model
  must be bound in that store. A missing admission refuses the start with
  `start_failed` / `launch_refused`, detail `breaker_admission_missing`; a failed
  bind uses detail `breaker_bind_failed:<reason>`. Neither refusal starts a unit.
  If the start times out while the bind waits, a successful late bind is retained,
  but that launch is abandoned. A same-key retry can reuse the same binding; another
  connection or model cannot reuse that run id. Start with a new client key to
  request a different route. Stop without a recorded cgroup remains
  `pending` / `cgroup_unknown`; it is not evidence that an engine is still alive.

  The store records a model as `routes.json` gives it when it is 1 to 128 printable ASCII
  characters with no space among them, so a vendor's own spelling such as `opus[1m]` is
  recorded as it is. A longer name, and one with a space, a control character or a non-ASCII
  character, cannot be recorded: the start is refused `start_failed` /
  `breaker_model_unrepresentable` before the route service is asked, and nothing is launched.
  A route with no model, or an empty one, is refused `request_invalid` by the route
  validator, as before.
  A connection id the store cannot key is refused at the same point, as
  `breaker_connection_unrepresentable`. Both look at the route itself and, with
  `"on_limit": "fallback"`, at every route in its `fallback` list; a list that is only shown
  to the user refuses nothing. `route.list` names either one as the route's blocker.
- **Routes.** `$XDG_CONFIG_HOME/recursion/routes.json` (operator-owned, not group or other
  writable) names the route per engine: `{"routes": {"claude_code": {"model": ...,
  "connection": {"kind": "subscription", "id": ...}, "api_key_helper": "absent",
  "deadline_s": 3600, "binary": "/abs/path/claude"}}}`. Nothing is defaulted: an engine
  with no entry answers `start_failed` / `route_not_configured`.

Not done: credential custody (an API-key route refuses, `no credential`; a Claude Code route
whose key comes from the user's `apiKeyHelper`, `"key_source": "api_key_helper"`, and a
`passthrough` route (Bedrock, Vertex, Foundry or a gateway) are refused `start_failed` /
`launch_refused`, `no credential custody`, before anything is launched, and so is a route whose
`limits` would switch to one (`"on_limit": "fallback"` with such a route in `fallback`);
`route.list` names that as the route's blocker; the subscription route is not affected), Pi, Hermes and
OpenCode, the Control MCP transport, a browser pairing step for the device token, a second
launch per run (a fallback route refuses `run_already_launched`), re-attaching the route
driver to a run that outlived a daemon (its output file keeps growing and is readable; its
route result is not produced), bar evidence for any route, the O7 interventions, a surface
that can approve or veto an effect, and an installer for the plugin files, the authority
ledger and the breaker store.

Also not done: defects found in review on Nov 12, 2026, left as they are on this branch for
the Stop lane on master.

- **One lock for every run's unit calls.** `Daemon.start`, `Daemon.stop`, `Daemon.reconcile`,
  `Daemon.cgroup_clear` and the whole of `Daemon.sweep` hold the daemon's one lock while they
  call the unit manager, and each `systemd-run` or `systemctl` call may wait up to 15 seconds
  (`SystemdUserManager._call`). A Stop or a reconcile of one run can make three of them in a row
  (the stop, a `show`, and a second `show` before an end is recorded), and wait up to 2 seconds
  per cgroup path for a kill in between. A start makes two (`systemd-run`, then `show`). A sweep
  keeps the lock for its whole pass: that work for every run not yet ended, then one
  `list-units`, then a stop for each leftover unit. While one of these waits on a slow manager,
  a Stop of any other run waits for the lock. It is answered late, and `pending` if its confirm
  window has run out by then. The wait changes when Stop answers, not what it reads: `stopped`
  still needs the row ended and the cgroup walked empty in that same call. The control API's
  per-run locks and the socket's reserved Stop connections do not shorten the wait.
- **A failed launch cannot be retried under the same run id.** The launcher creates
  `stdin.fifo`, `stdout.jsonl` and `stderr.log` in the run's directory without looking for an
  earlier attempt, and `Daemon.start` inserts the run's row the same way. When a launch fails,
  only `env.fifo` is removed. The control API relaunches a `start_failed` run under its old id
  when the same `client_key` is sent again, so a relaunch of an attempt that got that far fails
  on the files or the row it left.
- **A short write of the environment is not noticed.** The engine's environment goes to
  `env.fifo` as one frame in one nonblocking `os.write`, and the count that call returns is not
  checked. The frame may be up to 60,000 bytes, on the assumption that a FIFO buffers 64 KiB.
  Where a FIFO holds less, part of the frame is written, the launcher takes the drained FIFO
  for an environment that was read, and `engine_exec` rejects the truncated frame: the engine
  never runs, and the start may already have been answered as launched.
- **A live unit whose control record says `start_failed`.** A start that runs out of time gives
  its launch up under the lock the launcher publishes under (`RunContext.abandon`,
  `RunContext.launched`), so a launch that finishes after that is ended and never published.
  What is not done is recovery when such a unit is alive all the same, for example because the
  daemon died after the start answered `launch_timeout` and before the launcher ended the unit.
  The control record then says `start_failed` with no cgroup, `DaemonControl.reconcile` adopts a
  missing cgroup only for a record still `starting`, and `run.stop` answers `pending` /
  `cgroup_unknown` without reaching the unit.
- **A `start_error` can belong to a later attempt.** `DaemonControl` reads the starter's failure
  for the run after the control API has answered and let go of the run's start lock. A same-key
  retry that fails in between replaces it: the reply's `start_failed` and run id are right, and
  its `start_error` may be the later attempt's.

Evidence: `evidence_ns.sh` runs `evidence_e2e.py`, which drives the real daemon and CLI
against a real `systemd --user` manager with a stand-in engine
(`evidence_standin_engine.py`): start, stream, inspect, Stop, cleanup, restart, a signed
test plugin loaded through the plugin loader, the GUI inventory and an O7 read with the device
token, the TUI over the socket, and a breaker trip that survives SIGKILL and restart. The
daemon interpreter needs `cryptography`: set `RECURSION_EVIDENCE_PYTHON`, or put its path on
the first line of `./evidence-python` when the wrapper's environment is reset.

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
With neither injected, `main` composes its own (`control_adapter.compose_control`; the
`DeviceDoor` under "Composition"). A `LoopbackGUI` built with no door refuses authentication
on every request (401); it does not silently trust loopback, invent data, or manufacture a
token. Tests inject a fake API/device door and prove a real GET returns the page.
A browser pairing step for the device token is not done (see "Not done").

An evicted connection's complete Stop may still be dispatched if Stop capacity is
available; losing the HTTP connection does not confirm failure. Up to 18 requests
may be answering when Stops use the two reserved slots; saturation can refuse a
new Stop as well as reads. For an authenticated form on /stop or /action, EOF
before Content-Length gets 400. A request with more than one Content-Length, or
with a Transfer-Encoding header, gets 400 before the page sees it: the server
reads a body by its one length only. Closing the listener does not join in-flight
request threads.

An installed operator plugin is a directory (`manifest.json`, `manifest.sig`, `plugin.py`)
under `$XDG_CONFIG_HOME/recursion/plugins/`, or named with `--plugin <DIR>` (absolute). The
daemon no longer imports a `MODULE:REGISTRY` factory by name: every plugin goes through the
plugin loader (see "Plugins" above), and its `registry()` gives the plain `required`/`panels`
mappings that reach `recursion_core.gui.compose`; the plugin does not import Recursion. A
plugin that is a package is installed as a small `plugin.py` that imports it, and then only
that file's bytes are covered by the manifest digest and signature. The cross-repo tests take
the real plugin's factory from `RECURSION_CROSS_REPO_PLUGIN=pkg.module:registry` and skip when
it is unset. `/panel?id=...` renders only the named O7 reads, through the one shared
venue-data filter (`gui/panels.py` `filter_venue_rows`; the daemon has no copy of it), with
no panel order verb and no panel Stop. Run Stop stays in the core page.

Contract: this section was written against `contracts/PRODUCT.md` at
`055cc7a5c793b7135e5d0565dc4198cf96230531`, SHA-256
`e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf`. That version is
superseded; the pinned one is `d84cdd26` (`contract/PRODUCT.pin.json`).
