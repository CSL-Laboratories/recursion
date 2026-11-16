# Control MCP server (lane MCP)

An MCP client spawns `python -m recursion_core.control.mcp` and drives Recursion's run verbs through it.
The server speaks MCP over its stdin and stdout and forwards every tool call to the daemon's owner-only
control socket, the same socket and client (`control/client.py`) the control CLI uses. It has no Stop
path of its own: the stop tool forwards to `run.stop` and answers what it answered.

This covers the lane's first two slices: the stdio server (#395), and the tool table projected from the
published method table with the Task derivation (#416). What it covers and what it does not:

| | Built | Not yet |
|---|---|---|
| Protocol | revision `2025-06-18` (the legacy era) | `2026-07-28` with `server/discover`, the `_meta` envelope and Tasks |
| Tools | the published method table's rows marked `mcp.exposed` that the daemon implements, projected under their published names, marks and parameter schemas (`projection.py`); the four run tools of `control/mcp_tools.py`, unchanged, for a daemon from before the method table or where the published files are missing | a tool for a row the table does not mark exposed (no `approval.open` or `approval.answer`) |
| Tasks | the Task of a run and of a turn (`task.py`, `mcp.task.v1`), checked against every `task.derivation` vector | Tasks on the wire (`tasks/get`, `tasks/cancel`), and where a Task's log and pending approvals come from at run time |
| Caller | the daemon's same-user rule, as for the CLI | caller tokens, scopes, a failed-auth limit (lane CONTROL's interface; how a client gets and loses its token is not decided) |
| Entry | this module entry | a registered `recursion mcp` command |
| Checked against | scripted clients in the tests and the fixtures below | a real MCP client product, or a conformance tool: none was run |

## Starting

The process takes no arguments. Before it reads anything from stdin it runs the socket check
(`socket_check.py`): the platform has the Unix transport; the socket path is absolute; its folder
exists, is a real folder of this user with mode 0700 or narrower; the socket exists, is a socket of
this user with mode 0600 or narrower; and one read (`run.list`, `limit: 1`) gets an answer in the control
API's shape, which also proves the listener runs as this user and admits this process (a process inside a
run unit is not admitted). It proves the socket is sound and answered, not which program answered. If any
step fails it writes one JSON line to stderr, `{"code": "socket_check_failed", "reason": ..., "detail": ...}`,
nothing to stdout, and exits 1. The folder and socket steps are repeated before every forwarded call.

Then, still before it reads stdin, it chooses its tools (`projection.choose_tools`). It reads the published
files (`schemas/control/v1/`; a source tree has them, an installed package not until lane INSTALL ships them
as package data) and asks the daemon `methods.list`:

- `method_unknown` (a daemon from before the method table): the legacy four;
- `ok` in the published shape (`code`, `table_version` and `methods` exactly; each entry `name`,
  `implemented` and `beta`, the two booleans) with the published `table_version`: the projected tools,
  each parameter schema made self-contained;
- `ok` in that shape while the published files are missing: the legacy four, with the reason logged;
- anything else refuses: another answer, a `methods.list` call that raises, a listing of another shape,
  another `table_version`, or published files that cannot be read or are not the published shape (a file
  nested deeper than Python's stack included). The process writes one JSON line to stderr,
  `{"code": "tools_unavailable", "reason": ..., "detail": ...}`, nothing to stdout, and exits 1. An exit or
  an interrupt while choosing is not a refusal.

`isError` follows CONTROL's mapping (`control.api.SUCCESS_CODES`): today a stop's or an interrupt's
`pending`, and a stop's `ended`, read as errors though they are truthful answers; the structured answer
carries the code, and CONTROL is to change the mapping. The published `run.start` leaves `engine` optional;
a daemon without a routing start registrant answers a start without one `request_invalid`, and nothing runs.

Exit statuses: 0 when the client ends stdin (once the answers owed have been read) or stdout fails; 1 when
the socket check fails or no tools can be chosen (`tools_unavailable`); 2 for an argument; 3 for an
unexpected error while serving (one JSON line, `internal_error`); 130 when interrupted. An answer is never
dropped, so a client that ends stdin and stops reading stdout without shutting it keeps the process until it
reads again or stdout fails.
Ending stdin never stops a run: admitted calls finish and are answered, and the runs go on. The process
reads fd 0 and writes fd 1 and fd 2 directly (`stdio.py`): nothing is buffered in `sys.stdout` or
`sys.stderr`, so a client that stops reading, or closes stderr, ends the process with its status, not
with a failed final flush (120). Once the last answer is out it waits at most 2 s for stderr to take the
remaining diagnostics, so a client that never reads stderr does not keep it alive.

## The wire

`server.py` says what each method answers. In short:

- `initialize` always answers `2025-06-18`; `protocolVersion`, `capabilities` and `clientInfo` (with a
  string `name` and `version`) are required, as the 2025-06-18 schema says.
- `server/discover` (a dual-era client's probe) and every other unknown method answer `-32601` and the
  process keeps serving, so such a client falls back to `initialize`.
- `tools/list` and `tools/call` are refused until `initialize`. Invalid arguments are `-32602` and reach
  nothing; `arguments` may be left out (`{}`), and when present must be an object (`null` is refused). A
  call the control socket could never carry (a frame over its 1 MiB, or a string UTF-8 cannot encode, such
  as a lone surrogate, which JSON can carry) answers `control_api_unavailable` with `delivered: false`, and
  nothing is written (`socket_check.CheckedSocketAPI`).
- Request ids are strings or integers. A fraction or a boolean is not an id, so it can never name another
  request.
- Admission never makes the reader wait: a call takes one of 8 ordinary slots (the stop tool also one of
  2 reserved slots), or waits in a queue (16 places for ordinary calls, 8 for stop calls, served first),
  or is refused at once with `-32000` "Server busy" and `data.delivered: false` (nothing was sent). A slot
  is held until the call's answer is written, so slow stdout cannot pile up workers.
- The reader never writes to stdout, so slow stdout never stops it reading: a Stop behind a busy answer is
  still sent and a cancellation still applied. The answers it owes (refusals, `ping`, `initialize`,
  `tools/list`) go to a reply queue written by a thread of its own (`replies.py`), holding at most 1 MiB
  (or one answer larger than that, taken only into an empty queue). A client that stops reading stdout
  and goes on writing fills it; an answer that does not fit is dropped and its request treated as never
  received (each such request sent nothing to the daemon; a dropped `initialize` answer leaves the
  session uninitialized). The client times out, as 2025-06-18 says it should, and may send it again.
  Stderr gets two lines per stretch of drops (`replies_dropped`, `replies_resumed`).
- Nothing waits for stderr either. Every diagnostic goes to a queue delivered by a thread of its own
  (`diagnostics.py`, 64 KiB; the process writes fd 2 directly). A client that captures stderr and stops
  reading it loses lines, counted in a `diagnostics_dropped` line once stderr moves again, but the reader
  goes on reading and answering. The bound never drops the last line (`end_of_input` or
  `output_failed`); it is lost only if stderr takes nothing for 2 s at the end, or a log has failed.
- `notifications/cancelled`: a call not yet sent (waiting, or holding a slot before its worker sends it)
  is never sent; a sent call's answer is dropped; an answer already being written goes out (the
  cancellation came too late). The send is decided under the session's lock, so the logged outcome is
  what the daemon saw. Nothing is ever sent to the daemon for a cancellation. A cancellation names an id:
  MCP forbids reusing one in a session, and a client that does so anyway can have its newer call cancelled
  by a late cancellation of the earlier one.
- When stdout fails, nothing more is sent: waiting calls are dropped, queued answers discarded, and the
  session ends without waiting for more input.

Diagnostics go to stderr as JSON lines; stdout carries only protocol messages, one ASCII line each.

## Fixtures

`fixtures/legacy/<group>/<name>.json`, replayed by `tests/test_fixtures.py` through the real stdio loop:

```
{"format": "mcp-stdio-fixture/1", "name": "...", "protocol": "2025-06-18", "summary": "...", "source": "...",
 "api":   [{"method": "run.stop", "params": {...}, "answer": {...}}, ...],
 "steps": [{"send": {...} | "send_text": "..." | "send_hex": "..", "expect": [{...}, ...]}, ...],
 "after_end_of_input": [{...}, ...]}
```

Each step's bytes are written, then exactly its `expect` answers must arrive, in order. `api` is the
control API's side: the calls that must arrive, in order, each with the answer it gets. Nothing else may
be written or called. `framing/` is the line format and JSON-RPC shapes, `negotiation/` the lifecycle
and the version answer, `tools/` the tool list and calls.

## Tests

```
PYTHONPATH=core python -m unittest discover -s core/recursion_core/control/mcp/tests -t core
```

`test_stdio.py` runs the module as a child process against the real owner-only transport with the
real control API over the shared fakes (Linux; skipped where `SO_PEERCRED` is missing).
