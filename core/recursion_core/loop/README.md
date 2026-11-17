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
  removed from every line the loop writes. On Linux its text is also cleared from the environment block the
  process started with (`/proc/<pid>/environ`); the loop does not start if that cannot be done.
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
`protocol_error` and `closed`. `protocol.py` gives every field. Where a line shares a name with an event.v1
payload its fields are that payload's as far as the loop knows them; `protocol.py` lists what the driver maps
(the connection id, the bucket, the input digest, the stream offsets of text, the envelope; `retry_after_s`
and `spill` dropped; `turn_ended` is not a driver draft). Text is written when its model call ends, not while
it streams. Every model call has one `usage` line, a failed or interrupted one included, with what it had
measured (unknown, never 0, where nothing was), and every `turn_ended` follows the turn's total and the
session's running total (a known 0 for a turn that made no call; a loop error included).

`input_accepted` is written the moment the loop takes a message, before the model sees it, and never for a
message it did not take. A turn the model finishes ends `candidate_done`, or `done` once the task's
acceptance check passes; the harness, not the model, decides when work is done. An `interrupt` answered
`delivered: true` always comes before that turn's `turn_ended`, which is `interrupted` unless the model had
already finished the turn or the loop failed. Standard input is read on its own descriptor, never through
`sys.stdin`: after `close` the loop writes `closed` and exits 0, whether or not the driver keeps the pipe open.

## The pieces

| File | What it does |
|---|---|
| `providers/base.py` | The provider interface: one streamed call, typed results and typed failures |
| `providers/anthropic.py` | Anthropic's Messages API on the standard library: streaming, tool use, prompt caching |
| `providers/fake.py` | A scripted provider |
| `providers/sse.py` | The event-stream reader the providers share |
| `tools/registry.py` | Input checks and the one output cap, with spill, that every tool passes through |
| `tools/read.py`, `tools/search.py` | Reading files and folders; searching contents (in a child process). A tool runs only through `ToolRegistry.dispatch`, which checks its input, the approval rule and cancellation, scrubs and caps its output; the registry hands out a tool's spec, never its `run`, and a `run` called directly skips all of that |
| `tools/write.py`, `tools/edit.py`, `tools/files.py` | Changing files: read first, unchanged since, atomic |
| `tools/shell.py`, `tools/reaper.py` | One command in its own process group and session, with an environment built from a short list of names; on Linux under a keeper that is the subreaper of all it starts. One deadline covers the command and the reading of its output; when it ends, times out or is interrupted, everything it started is stopped |
| `tools/workspace.py` | Path confinement, the spill store, and the record of what was read |
| `permissions.py` | Asking before a change, and the deadline that denies |
| `done.py`, `instructions.py` | The acceptance step; the instruction files |
| `engine.py`, `protocol.py` | The turn loop, and the input and output lines |

Still to come in this package: compaction; retries of an overloaded call; resume; the driver
(`routes/engines/recursion_loop.py`).
