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
| `test_opencode_route.py` | the one-shot drive on today's route path (`RouteService`, the breaker, the profile adapter and its guard), with a fake launcher |
| `test_opencode_driver.py` | `OpenCodeDriver` on driver.v2's gate (#376), with a spy for the spawn |
| `test_opencode_serve.py` | `ServeDriver` on driver.v2's gate, with scripted processes, a recorded abort and HTTP servers of the test's own on 127.0.0.1 |

The five test modules live here only until the ownership map gives this lane a path under
`routes/engines/tests/`. This folder has no `__init__.py` (the parent `fixtures/` is lane DRIVER-CC's), so
`unittest discover` does not find them; the hosted gate does (its glob `core/**/test_*.py` runs each file by its
entry point). Run them by name from the repository root:

```
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_parser
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_replay
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_route
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_driver
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_serve
```

## The module's interface (`routes/engines/opencode.py`)

Nothing registers any of it and no other lane imports it yet; this is what a consumer may rely on in this slice. `start` and `resume` take #376's PROVISIONAL forms (`start(session, workspace, env)`), which may still change there.

| Name | What it is |
|---|---|
| `Parser(*, connection_id, model, wall=time.time, clock=time.monotonic)` | one run's stdout. `connection_id` is `<kind>:<id>` (`ValueError` otherwise); `model` is `None` or passes `model_ok`. `wall` reads Unix seconds and dates event.v1's `reset_at`; `clock` is the breaker's clock and dates the route contract's limit. Not shared between threads. |
| `Parser.feed(line) -> list[dict]` | the drafts for ONE line (bytes, bytearray, memoryview or str; a str is encoded as UTF-8). Offsets count the bytes given, from 0 for each parser; a reader that restarts keeps its own offset. `ValueError` for two lines at once (a LF or CR anywhere but its terminal `
` or `
`), `TypeError` for another type, `RuntimeError` once `end` has returned. If an injected clock raises, or the call is interrupted once before it has returned, the line counts as unread; a second interruption that lands inside that rollback is not recovered. |
| `Parser.end(exit_code) -> list[dict]` | the `result` draft. `exit_code` is the process's status, or `None` when none was read: then the run ended well (`code` null) only if its latest step finished with reason `stop` and no later step started, and is `engine_exited` otherwise. `ValueError` for a non-int; `RuntimeError` once an `end` has returned. An `end` interrupted once before it has returned leaves the run open, so `end` may be called again; a second interruption that lands inside that rollback is not recovered. |
| `Parser.route_result(request_id) -> dict` | the same run as the route contract's result (`recursion.route/v0`), its limit's `reset_at` on the breaker's clock; each `usage` event's `data` holds event.v1's `{value, quality, provenance}` per count. Every call returns fresh copies. `ValueError` for an empty or non-string id; `RuntimeError` until `end` has returned. |
| `Parser.text`, `.engine_session_id`, `.anomalies` | the answer (model payload), the engine's own session id (first one seen), and what was refused on each line and why; a line listed there may still have given its `engine.output`. Only a `step_finish` of the run's own session (or one naming none) counts as the run's latest finished step. |
| `Parser.outcome -> tuple[str \| None, str] \| None` | the run's `(code, detail)` once `end` has returned, else `None` |
| `Parser.started`, `Parser.lines_read` | `started`: the line just fed was a well-formed `step_start` of the run's own session (reset by every line, put back by `feed`'s rollback); `lines_read`: the lines fed so far (an empty feed is none) |
| `declaration() -> dict` | driver.v2's root record for the one-shot drive |
| `build_argv(model, *, binary=None) -> list[str]` | `opencode run --format json --model <model>`, or no `--model` for a null model (OpenCode's own default); argv[0] is `binary` or `opencode`. `ValueError` for a model `model_ok` refuses or a binary that is not a string, is empty or only white space, starts with `-`, or holds a control character. |
| `model_ok(model) -> bool`, `input_digest(tool_input) -> str` | the one model rule; the RFC 8785 digest (`ValueError` when the input has no RFC 8785 text) |
| `held_to_the_guard(model, profile) -> str | None` | the one place a launched model is worked out. Refusals are the guard's `SubscriptionRouteRefused`: `model_malformed` (not a plain `str`, or `model_ok` refuses it), `model_differs_from_profile` (the profile names another model), `claude_subscription_provider` (a Claude subscription provider id or Claude-login spelling, from any source), `model_provider_mismatch` (a model the profile does not name, of another provider than the profile's). `profile` None is the driver path; a profile that is not a mapping counts as empty. A null model holds nothing: OpenCode's own default runs. |
| `route_argv(request, *, binary=None) -> list[str]` | `ProfileRouteAdapter`'s argv hook: `build_argv` on `held_to_the_guard`'s model, raised before the guard and the launcher, so the adapter answers each refusal with a typed `subscription_route_refused`. `RouteError(request_invalid, "route")` for a request with no route mapping. Today's route service refuses a null model itself (`request_invalid`, `route.model`) before any adapter. |
| `OneShot(clock, *, wall=time.time)(process, request, deadline_at, events) -> dict` | the one-shot drive the adapter calls after the guard. The deadline is checked before the prompt is written, before every read and after every read (the end of output included): `Deadline`. A chunk that is not text, or holds a LF or CR anywhere but its terminal `\n` or `\r\n`: protocol error. At the end of stdout the process is handed to cleanup (`fence("stdout_ended")`) before the result is returned. An `OSError` from the process leaves unfenced (the adapter fences it, `io_error`). Any other exception after the start fences the process (`driver_error`) and leaves unchanged. `ValueError` at construction for a clock that cannot be called. |
| `OpenCodeDriver(spawn, *, binary=None, wall=, clock=)` | driver.v2 for the one-shot drive on #376's gate: `start` spawns `opencode run`; the one `user_message` is the prompt on standard input, then closed; `parse` reads stdout and accepts the prompt on the first well-formed `step_start` of the run's own session read by a `parse` call after the prompt was written. A write that raised is never accepted (stricter, on purpose, than `routes.driver`'s DELIVERY paragraph: its delivery stays pending); a close that raised is tried again by a later close, an explicit `close` included. A message reserved after `finish` or `release` is `DriverStateError` with its turn ended again. `finish`, `release` and the reading of a line hold one driver lock, taken and released in one protected region (`control.hooks`' OWNERSHIP form): one interruption leaves it free. Every other verb is refused by the gate before anything starts. `ValueError` at construction for something it cannot call or a binary `build_argv` refuses; `DriverStateError` for a handle it does not hold. |
| `OpenCodeDriver.finish(handle, exit_code) -> list[dict]` | the `result` draft once the process has exited: one terminal transition per handle (finishers serialised; one gets the result, any other `DriverStateError`; interrupted, it completes when called again, the first exit code deciding; interrupted after the worker was removed, the result is lost). It waits for a line being parsed, so a 429 in it decides the result. `DriverInputError` for an `exit_code` that is not an int or None, with nothing changed. The process is the caller's: `finish` ends no process. |
| `OpenCodeDriver.release(handle)` | forgets a handle whose `finish` will never come and ends its turn in progress; a handle it does not hold is left as it is. It waits for a finish in progress. The process is the caller's to end (lane STOP's and DRIVER-CC's to define). |
| `ENGINE`, `DEFAULT_EXECUTABLE`, `CHILD_ENV`, `FLAGS`, `KNOWN_ERRORS` | constants; `FLAGS` is read-only |
| `ServeDriver(spawn, *, binary=None, port=pick_port, abort=http_abort, ready_s=30.0, wall=, clock=)` | the serve drive on driver.v2's gate (#376), built against fakes and registered nowhere: one `opencode serve` per handle on 127.0.0.1, one `opencode run --attach` per turn, `--session <id>` for every turn after OpenCode reported its conversation id and for every turn of a resumed handle. `finish_turn(handle, exit_code)` drafts a turn's result and ends the turn; `interrupt` asks the server to abort, else terminates the turn's process (also when the ask is cut short by a `BaseException`, which then leaves as itself); `release(handle)` is terminal: every later call is `DriverStateError`, the turn in progress ends, the turn's process and the server are terminated each on its own, a process still being spawned is terminated unwritten when the spawn returns, and a termination that raised is tried again by the next `release`. A reported session id that a later turn could not pass is not adopted (`session_id_unusable` in the turn parser's `anomalies`). `start` raises `DriverStateError` when the server gives no readiness line for its port in `ready_s` seconds. |
| `serve_argv(port, *, binary=None)`, `attach_argv(url, model, *, session=None, binary=None)`, `listening_url(text, port)`, `server_url(port)`, `pick_port()`, `http_abort(url, session, *, timeout=5.0)` | the serve drive's parts. Only `http://127.0.0.1:<port>` is served or attached (`ValueError` otherwise); a session id that is empty, starts with `-`, is `.` or `..`, or holds a control character is refused (`ValueError`). |
| `SERVE_FLAGS`, `serve_declaration()` | the serve drive's flags (multi_turn, interrupt, resume, typed_limits, usage) and its driver.v2 root record |
