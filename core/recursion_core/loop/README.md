# recursion_loop: the core's own model loop

Recursion drives the coding CLIs a machine has configured, and it also runs its own model loop on the user's
own API key. This package is that loop. It runs as an engine, like the CLIs: its own process (never inside
the daemon), JSON lines in on standard input, typed JSON lines out on standard output, and its key and base
URL only from its environment.

## Run it

```
python -m recursion_core.loop --provider anthropic --model <the route's model id> --home <private folder>
```

- `ANTHROPIC_API_KEY` and, optionally, `ANTHROPIC_BASE_URL` come from the environment. The key is removed
  from the process environment once the provider holds it, so no child of the loop inherits it, and it is
  removed from every line the loop writes.
- `--home` is the engine's private folder (created with mode 0700 if missing; refused if other users can
  open it). Outputs too large to show are saved in `<home>/spill/`.
- The tools work in the current folder, or in `--workspace`.
- `write`, `edit` and `shell` ask before they run (a `permission_request` line; the answer comes back on
  standard input) and are denied when no answer comes by `--approval-timeout` (900 s by default).
  `--permissions full_access --acknowledge-full-access` skips the asking for one run.
- `--acceptance <command>` is the task's acceptance check: the turn is `done` only when it passes.
- `AGENTS.md` and `CLAUDE.md` from the repository root down to the workspace are read into the prompt
  (32 KiB in all) unless `--no-project-instructions`; the `ready` line lists them.
- `--provider fake --fake-script <steps.json>` runs a scripted model instead (tests and conformance runs).

## The protocol

Inputs are driver.v2's inputs plus `interrupt`: `user_message`, `approval_answer` (with the loop's own
`request_id`, and optionally the bound input), `interrupt` and `close`. Outputs are `ready`,
`input_accepted`, `text`, `tool_call`, `permission_request`, `permission_expired`, `tool_result`, `usage`
(per call, per turn and the session's running total), `limit`, `acceptance`, `turn_ended`, `interrupt`,
`protocol_error` and `closed`. `protocol.py` gives every field. The payload fields match event.v1's payloads
of the same name, so the driver adds only the connection id, the bucket, the input digest and the envelope.

`input_accepted` is written the moment the loop takes a message, before the model sees it, and never for a
message it did not take. A turn the model finishes ends `candidate_done`, or `done` once the task's
acceptance check passes; the harness, not the model, decides when work is done.

## The pieces

| File | What it does |
|---|---|
| `providers/base.py` | The provider interface: one streamed call, typed results and typed failures |
| `providers/anthropic.py` | Anthropic's Messages API on the standard library: streaming, tool use, prompt caching |
| `providers/fake.py` | A scripted provider |
| `providers/sse.py` | The event-stream reader the providers share |
| `tools/registry.py` | Input checks and the one output cap, with spill, that every tool passes through |
| `tools/read.py`, `tools/search.py` | Reading files and folders; searching contents (in a child process) |
| `tools/write.py`, `tools/edit.py`, `tools/files.py` | Changing files: read first, unchanged since, atomic |
| `tools/shell.py` | One command in its own process group, stopped at its timeout or on an interrupt |
| `tools/workspace.py` | Path confinement, the spill store, and the record of what was read |
| `permissions.py` | Asking before a change, and the deadline that denies |
| `done.py`, `instructions.py` | The acceptance step; the instruction files |
| `engine.py`, `protocol.py` | The turn loop, and the input and output lines |

Still to come in this package: compaction; retries of an overloaded call; resume; the driver
(`routes/engines/recursion_loop.py`).
