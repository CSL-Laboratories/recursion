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
  `subprocess`. `recursion-control conditions` (CSL-799) reads `run.list` and prints the conditions it
  exposes with the TUI's labels and words (`{"code": ..., "conditions": [{"label", "text"}]}`); the TUI
  has the same command, and no control API method was added.
- `mcp_tools.py`: Control MCP tool descriptors with every annotation set explicitly, and `call_tool`,
  which forwards to the control API:

  | Tool | readOnlyHint | destructiveHint | idempotentHint | openWorldHint |
  |---|---|---|---|---|
  | `recursion_run_list`, `recursion_run_status` | true | false | true | false |
  | `recursion_run_stop` | false | true | true | false |
  | `recursion_run_start` | false | false | false | true |

  Stop carries `destructiveHint: true` per 398cd812; the older CSL-762/F60 handoff wording
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

## Tests

From the repository root:

```
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/control/tests -t core -v
PYTHONPATH=core python3.14t -X gil=0 -B -m unittest discover -s core/recursion_core/control/tests -t core -v
```
