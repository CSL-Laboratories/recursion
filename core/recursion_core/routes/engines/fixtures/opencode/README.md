# OpenCode fixtures (lane ENGINE-E3)

What `opencode run --format json` prints, for the parser in `routes/engines/opencode.py`.

**Every `.jsonl` file here is SYNTHETIC**: written by hand from reads of OpenCode's source at two tags, never
captured from a real run. `index.json` lists each file with `"synthetic": true`, the package line it stands for,
the exit status it would end with, what it shows, and the drafts the parser must give for it; its `sources` list
cites the source lines each shape was read from. No OpenCode binary ran, nothing was signed in and no login store
was read. No captured fixture may be listed yet: the test refuses an entry with `"synthetic": false` until the
first capture lands in a reviewed change of its own. That change will name the file `captured-...` (the only name
the test admits for such an entry) and drop the test's no-capture-yet line.

| File | What it is |
|---|---|
| `index.json` | the fixtures, their sources and the expected drafts |
| `synthetic-v1-*.jsonl` | `opencode-ai` 1.19.5 (anomalyco/opencode 810c0eba) |
| `synthetic-v2-*.jsonl` | `@opencode/cli` 2.2.6 (anomalyco/opencode dc2ffcf8) |
| `fake_opencode.py` | a stand-in that replays one fixture byte for byte when given the parser's argv, the prompt on standard input and `OPENCODE_DISABLE_AUTOUPDATE=1` |
| `test_opencode_parser.py` | the parser against every fixture, plus single lines |
| `test_opencode_replay.py` | the fake run as a process: the same drafts come back through a pipe |

The two test modules live here only until the ownership map gives this lane a path under
`routes/engines/tests/`. This folder has no `__init__.py` (the parent `fixtures/` is lane DRIVER-CC's), so
`unittest discover` does not find them; the hosted gate does (its glob `core/**/test_*.py` runs each file by its
entry point). Run them by name from the repository root:

```
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_parser
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_replay
```

## The parser's interface (`routes/engines/opencode.py`)

Nothing registers it and no other lane imports it yet; this is what a consumer may rely on in this slice.

| Name | What it is |
|---|---|
| `Parser(*, connection_id, model, wall=time.time, clock=time.monotonic)` | one run's stdout. `connection_id` is `<kind>:<id>` (`ValueError` otherwise); `model` is `None` or passes `model_ok`. `wall` reads Unix seconds and dates event.v1's `reset_at`; `clock` is the breaker's clock and dates the route contract's limit. Not shared between threads. |
| `Parser.feed(line) -> list[dict]` | the drafts for ONE line (bytes, bytearray, memoryview or str; a str is encoded as UTF-8). Offsets count the bytes given, from 0 for each parser; a reader that restarts keeps its own offset. `ValueError` for two lines at once, `TypeError` for another type, `RuntimeError` once `end` has returned. If an injected clock raises, or the call is interrupted once before it has returned, the line counts as unread; a second interruption that lands inside that rollback is not recovered. |
| `Parser.end(exit_code) -> list[dict]` | the `result` draft. `exit_code` is the process's status, or `None` when none was read (`engine_exited`). `ValueError` for a non-int; `RuntimeError` once an `end` has returned. An `end` interrupted once before it has returned leaves the run open, so `end` may be called again; a second interruption that lands inside that rollback is not recovered. |
| `Parser.route_result(request_id) -> dict` | the same run as the route contract's result (`recursion.route/v0`), its limit's `reset_at` on the breaker's clock; each `usage` event's `data` holds event.v1's `{value, quality, provenance}` per count. Every call returns fresh copies. `ValueError` for an empty or non-string id; `RuntimeError` until `end` has returned. |
| `Parser.text`, `.engine_session_id`, `.anomalies` | the answer (model payload), the engine's own session id (first one seen), and the lines that were not read and why |
| `declaration() -> dict` | driver.v2's root record for the one-shot drive |
| `build_argv(model, *, binary=None) -> list[str]` | `opencode run --format json --model <model>`; argv[0] is `binary` or `opencode`. `ValueError` for a model `model_ok` refuses or a binary with a control character. |
| `model_ok(model) -> bool`, `input_digest(tool_input) -> str` | the one model rule; the RFC 8785 digest (`ValueError` when the input has no RFC 8785 text) |
| `ENGINE`, `DEFAULT_EXECUTABLE`, `CHILD_ENV`, `FLAGS`, `KNOWN_ERRORS` | constants; `FLAGS` is read-only |
