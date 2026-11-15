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
- `--provider fake --fake-script <steps.json>` runs a scripted model instead (tests and conformance runs).

## The protocol

Inputs are driver.v2's inputs plus `interrupt`: `user_message`, `approval_answer`, `interrupt` and `close`.
Outputs are `ready`, `input_accepted`, `text`, `tool_call`, `tool_result`, `usage`, `limit`, `turn_ended`,
`interrupt`, `protocol_error` and `closed`. `protocol.py` gives every field. Where a line shares a name
with an event.v1 payload its fields are that payload's as far as the loop knows them; `protocol.py` lists
what the driver maps (the connection id, the bucket, the input digest, the stream offsets of text, the
envelope; `retry_after_s` and `spill` dropped; `turn_ended` is not a driver draft). Text is written when its
model call ends, not while it streams. Every model call has one `usage` line, a failed or interrupted one
included, with what it had measured (unknown, never 0, where nothing was).

`input_accepted` is written the moment the loop takes a message, before the model sees it, and never for a
message it did not take. A turn the model finishes ends `candidate_done`; the harness, not the model,
decides when work is done. An `interrupt` answered `delivered: true` always comes before that turn's
`turn_ended`. Standard input is read on its own descriptor, never through `sys.stdin`: after `close` the
loop writes `closed` and exits 0, whether or not the driver keeps the pipe open.

## The pieces

| File | What it does |
|---|---|
| `providers/base.py` | The provider interface: one streamed call, typed results and typed failures |
| `providers/anthropic.py` | Anthropic's Messages API on the standard library: streaming, tool use, prompt caching |
| `providers/fake.py` | A scripted provider |
| `providers/sse.py` | The event-stream reader the providers share |
| `tools/registry.py` | Input checks and the one output cap, with spill, that every tool passes through |
| `tools/read.py`, `tools/search.py` | Reading files and folders; searching contents (in a child process). A tool runs only through `ToolRegistry.dispatch`, which checks its input first; its `run` called directly assumes checked input |
| `tools/workspace.py` | Path confinement, the spill store, and the record of what was read |
| `engine.py`, `protocol.py` | The turn loop, and the input and output lines |

Still to come in this package: write, edit and shell behind the approval queue; instruction files;
the acceptance step that turns `candidate_done` into `done`; compaction; resume; the driver
(`routes/engines/recursion_loop.py`).
