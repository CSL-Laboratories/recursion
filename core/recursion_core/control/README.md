# control: control API, CLI and Control MCP descriptors with truthful Stop (CSL-798)

Candidate code for build item 8. Spec: PRODUCT.md at csl-research `398cd812cb8a9cf0ef3d1950f1b15bd9a76e3dfa`
(SHA-256 `36666beeadacb2af741db0ee55022b2e3911513b4de329ec500d50badbb3a226`): 3.4 S6 (Stop), 11.3 (#916
procedure), 11.8 (control API), O41 (Codex control-tool posture, decided Nov 7 "All picks"; Q48 is
recorded on F60, CSL-762).

## What it is

- `api.py`: `ControlAPI.call(method, params) -> dict` with `run.start`, `run.status`, `run.list` and
  `run.stop`, each answering with a typed code. Every surface (this CLI now, the GUI and Control MCP
  later) goes through it; none has its own Stop path.
- Stop: the first `run.stop` asks the injected cleanup layer (`KillTree`, a cgroup v2 + pidfd
  abstraction running the #916 procedure) to stop the run's tree, then to confirm. Only a confirmed
  empty cgroup answers `stopped`. A populated cgroup, a confirm timeout, an unknown answer or a cleanup
  error answers `pending` with that reason, never `stopped` or `terminated`. A repeat stop on a stopped
  run sends nothing; on a pending run it only re-checks. An unknown run is `run_unknown`.
- Reads (`run.status`, `run.list`) return copies, change no state and never call the cleanup layer.
- `registry.py`: the `RunRegistry` and `KillTree` interfaces, plus an in-memory registry for tests.
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

  Stop carries `destructiveHint: true` per 398cd812; the older CSL-762/F60 handoff wording
  (`destructiveHint: false` on start and stop) is superseded.
- `mcp_dual_era.py` (CSL-766, B5 / F56): an offline reading of Claude Code
  ``2.1.312`` and MCP spec ``38353251fab3bdeead03dd8283b9e66076ce1a7f``. Default
  (unset or ``auto``) negotiates ``2026-07-28`` via ``server/discover`` when the
  server supports it; ``MCP_PROTOCOL_NEGOTIATION=legacy`` initializes on
  ``2025-06-18``. A dual-era mock server speaks both over one ``ControlAPI`` so
  the same run id and Stop are visible from either era (n=5). Native Tasks
  ``CreateTaskResult`` is returned from ``tools/call`` only when the request
  lists ``io.modelcontextprotocol/tasks``. ``tasks/list`` and
  ``notifications/cancelled`` are not served. ``tools/list`` returns the four
  control tool descriptors in both eras. With ``auth_token`` set, ``tools/list``,
  ``tools/call`` and ``tasks/*`` need ``_meta.authorization`` before anything
  reaches ``ControlAPI`` (only the handshakes are open). An unknown ``taskId`` is
  a JSON-RPC ``-32602`` ``run_unknown`` error, and ``tasks/cancel`` sends nothing
  to the Stop path for it. Task ``createdAt`` comes from the run's
  ``started_at``; the default clock is wall time. Live binary wire-capture is
  unmeasured (no Claude Code is spawned).
- `codex_config.py`: `codex_operator_snippet(server_id, command, args)` writes the operator's Codex TOML,
  setting `approval_mode = "approve"` under `mcp_servers.<id>.tools.recursion_run_stop`, so stop does not
  prompt where that setting is in place. `strict_auto_review` or a managed approval requirement can still
  force a prompt. Without the setting, stop prompts in Auto mode and is refused under `codex exec`. Start
  is not auto-approved.

## Candidate and unwired

No daemon serves this API yet, no MCP transport is implemented, the CLI has no packaged entry point (its
API client factory is injected) and no real cleanup layer is wired. Nothing here talks to a real Codex or
Claude Code client, a provider or the network; tests use fakes only.

## Tests

From the repository root:

```
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/control/tests -t core -v
PYTHONPATH=core python3.14t -X gil=0 -B -m unittest discover -s core/recursion_core/control/tests -t core -v
```
