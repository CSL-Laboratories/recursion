# control: control API, CLI and Control MCP descriptors with truthful Stop (CSL-798)

Candidate code for build item 8. Spec: PRODUCT.md at csl-research `055cc7a5c793b7135e5d0565dc4198cf96230531`
(SHA-256 `e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf`, receipt pointer CSLR-D-011; first written
against `398cd812`, whose 3.4 S6, 11.3, 11.8 and O41 text 055cc7a5 keeps): 3.4 S6 (Stop), 11.3 (#916
procedure), 11.8 (control API), O41 (Codex control-tool posture, decided Nov 7 "All picks"; Q48 is
recorded on F60, CSL-762).

## What it is

- `api.py`: `ControlAPI.call(method, params) -> dict` with `run.start`, `run.status`, `run.list` and
  `run.stop`, each answering with a typed code. Every surface (this CLI now, the GUI and Control MCP
  later) goes through it; none has its own Stop path.
- Stop: the first `run.stop` asks the injected cleanup layer (`KillTree`, a cgroup v2 + pidfd
  abstraction running the #916 procedure) to stop the run's tree, then to confirm. Only a confirmed
  empty cgroup answers `stopped`, and only after that cleanup request was sent for the run's cgroup.
  A populated cgroup, a confirm timeout, an unknown answer or a cleanup
  error answers `pending` with that reason, never `stopped` or `terminated`. A repeat stop on a stopped
  run sends nothing; on a pending run it only re-checks, unless the earlier cleanup request was never
  sent (it raised, or the stop object does not record `requested: true`), in which case the next stop sends it again. An unknown run is `run_unknown`. A start's
  `client_key` dedupes retries per tenant and is bound to that start's engine and prompt digest; reusing
  it for a different request is `request_invalid` / `client_key_reused`. The key and digest are stored in
  the run record (`start_key`) and found through `RunRegistry.find_start`, so a durable registry keeps
  dedupe across a daemon restart (11.3, B-17).
- Launch intent first: `run.start` picks the run id, writes the record as `starting` (with its
  `start_key`) and only then calls the injected starter with that `run_id`. If that first write fails,
  nothing is launched (`registry_unavailable`). If the write after launch fails, the answer is
  `start_unconfirmed` and the record stays `starting`, but the API keeps the starter's cgroup in memory:
  `run.stop` aims the cleanup layer at it (even when the registry cannot be read at all, answering
  `"recorded": false`), and a same-key retry records it once the registry answers.
  A `run.stop` that arrives before the launch is recorded answers `pending` / `cgroup_unknown` and is
  kept as a stop intent; the start acts on it once the launch is recorded and returns that stop result
  under `stop`. A same-key retry never upgrades an unconfirmed or failed start to `started`: a starter
  that raised is relaunched under the same run id (unless a Stop was asked for it, which answers
  `start_failed` / `stop_requested`), and a `starting` record with no known cgroup (the starter handed
  back none, or a daemon restart lost the in-memory cgroup) answers `start_unconfirmed`. Reconciling such
  a record against the host after a restart (the startup sweep) belongs to the durable-registry item,
  CSL-738.
- Registry errors never escape `ControlAPI.call`: a read, a stop lookup or the launch-intent write that
  fails answers `registry_unavailable`. Once the cleanup layer has answered, Stop returns `stopped` or
  `pending` even if writing that back fails, with `"recorded": false`; the next stop then sees the old
  record and asks the cleanup layer again, which is safe to repeat. For a launch whose own write failed,
  the API keeps the Stop decision with that launch, so a later Stop or a same-key retry continues it
  (re-sends a request that raised, writes back a confirmed `stopped`) and never records a bare `running`
  over it. A failed start whose record cannot be re-read writes nothing, so it never erases a Stop
  intent recorded during the launch.
- Stop decisions and launch records are serialised per run, not API-wide, so one hung cleanup never
  queues another run's Stop (11.3, D2-55 / B-19), and a launch never overwrites a Stop that landed
  during it.
- Starts are serialised only per `(tenant, client_key)`, so a slow or hung starter delays only a retry
  of that same start; a start without a key, or with another tenant or key, never waits on it.
- **Unscoped (open decision).** `ControlAPI.call` takes no caller identity. `run.list` and `run.status`
  return every tenant's records, and `run.stop` stops any run by id, whoever asks. Tenant is only a
  namespace for `client_key`, not an access boundary. Caller scopes (11.8 / D3) are not built here.
- Reads (`run.status`, `run.list`) return copies, change no state and never call the cleanup layer.
- `registry.py`: the `RunRegistry` (`get`, `list`, `put`, `find_start`) and `KillTree` interfaces, plus an
  in-memory registry for tests.
  The durable registry (B8, B9) and the real cgroup/pidfd cleanup (B6) are other build items.
- `cli.py`: `recursion-control list | status RUN | stop RUN | start --tenant --engine --prompt`. It
  makes one API call, prints the API's answer verbatim as JSON and exits 0 (ok, started, stopped),
  3 (pending), 4 (run_unknown), 1 (other error) or 2 (usage). It imports no `os`, `signal` or
  `subprocess`.
- `mcp_tools.py`: Control MCP tool descriptors with every annotation set explicitly, and `call_tool`,
  which forwards to the control API:

  | Tool | readOnlyHint | destructiveHint | idempotentHint | openWorldHint |
  |---|---|---|---|---|
  | `recursion_run_list`, `recursion_run_status` | true | false | true | false |
  | `recursion_run_stop` | false | true | true | false |
  | `recursion_run_start` | false | false | false | true |

  Stop carries `destructiveHint: true` per 055cc7a5 O41; the older CSL-762/F60 handoff wording
  (`destructiveHint: false` on start and stop) is superseded.
- `codex_config.py`: `codex_operator_snippet(server_id, command, args)` writes the operator's Codex TOML,
  setting `approval_mode = "approve"` under `mcp_servers.<id>.tools.recursion_run_stop`, so stop does not
  prompt where that setting is in place. `strict_auto_review` or a managed approval requirement can still
  force a prompt. Without the setting, stop prompts in Auto mode and is refused under `codex exec`. Start
  is not auto-approved.

## Candidate and unwired

No daemon serves this API yet, no MCP transport is implemented, the CLI has no packaged entry point (its
API client factory is injected) and no real cleanup layer is wired. Nothing here talks to a real Codex or
Claude Code client, a provider or the network; tests use fakes only.

**Not built here, owner not assigned: the daemon adapter.** This PR does not build the production
adapter that makes the daemon (recursion #324, CSL-738) construct a `ControlAPI`, hand its client to
this CLI, and give the GUI a device/pairing door onto it. #324's body says #327 owns that adapter; this
PR does not take it on, and no other lane or issue is named for it yet. Until one is, the daemon's GUI
door answers 401 and `cli.run(..., None)` answers `control_api_unavailable`, so Stop through the GUI or
the packaged CLI on Nov 11 (PRODUCT.md 2.1, 3.4 S6) has no owner. Which lane and issue own it is
Lucas's call.

## Tests

From the repository root:

```
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/control/tests -t core -v
PYTHONPATH=core python3.14t -X gil=0 -B -m unittest discover -s core/recursion_core/control/tests -t core -v
```
