Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E8.

# Pi fixtures (engine E8)

Everything in this folder is **SYNTHETIC**: written by hand (with the helpers in `fake_pi.py`) from Pi's
own protocol pages at `earendil-works/pi@bb65e3e49710fbd14f10c7792d0eaa6e2b2c5923`
(`packages/coding-agent/docs/json.md`, `rpc.md`, `rpc-commands.md`, `message-types.md`, and
`src/modes/json-event.ts`, `src/modes/print-mode.ts`). No Pi binary was installed, started or signed in
to. Every data file's name starts with `synthetic-`. The model, provider, session id, paths and numbers
in them are made up.

| File | What it is |
|---|---|
| `synthetic-json-basic.stdout.jsonl` | stdout of `pi -p --mode json`: header, one plain answer, settled |
| `synthetic-json-basic.expected.json` | the drafts, signals and session id the parser gives for it |
| `synthetic-json-tools.stdout.jsonl` | one-shot stdout with two tool calls (one fails), a `queue_update`, the final answer |
| `synthetic-json-tools.expected.json` | its drafts, signals and session id |
| `synthetic-json-error-quota.stdout.jsonl` | one-shot stdout whose model call fails with quota-sounding text, retried once by Pi, then given up |
| `synthetic-json-error-quota.expected.json` | its drafts (the result is `short_fault`), signals and session id |
| `synthetic-rpc-two-turns.transcript.jsonl` | `pi --mode rpc`: `get_state`, then two prompts to the same worker |
| `synthetic-rpc-two-turns.expected.json` | its drafts, signals and session id |
| `synthetic-rpc-steer-abort.transcript.jsonl` | RPC: a prompt, a steer while it runs, an abort, then a new prompt to the same worker |
| `synthetic-rpc-steer-abort.expected.json` | its drafts, signals and session id |

Forms:

* `*.stdout.jsonl` is Pi's stdout byte for byte as Pi would write it (JSON.stringify: no spaces, whole
  numbers without a fraction, one record per LF).
* `*.transcript.jsonl` is one step per line: `{"to":"pi","record":{...}}` for a line the driver writes
  (a prompt also carries `"delivery"`, the delivery id, turn id and `how` it was written for) and
  `{"from":"pi","record":{...}}` for a record Pi writes.
* `*.expected.json` is `{"engine_session_id", "drafts", "signals"}` as `routes/engines/pi.py` gives them.

The tests are here too, for now, because `routes/engines/tests/` is lane DRIVER-CC's path:
`test_pi_parser.py`, `test_pi_fixtures.py`, `test_pi_fake.py`. Run each by dotted name from the
repository root, for example
`PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.pi.test_pi_fixtures`.
`unittest discover` does not enter this folder (it has no `__init__.py`).

When Lucas's attended run is captured (his own install, his own sign-in), its redacted stdout and
transcript go here under names without `synthetic-`, with their expected drafts, and replay through the
same `replay` in `test_pi_fixtures.py`.
