# Control MCP server (lane MCP)

An MCP client spawns `python -m recursion_core.control.mcp` and drives Recursion's run verbs through it.
The server speaks MCP over its stdin and stdout and forwards every tool call to the daemon's owner-only
control socket, the same socket and client (`control/client.py`) the control CLI uses. It has no Stop
path of its own: the stop tool forwards to `run.stop` and answers what it answered.

This is the lane's first slice. What it covers and what it does not:

| | In this slice | Not yet |
|---|---|---|
| Protocol | revision `2025-06-18` (the legacy era) | `2026-07-28` with `server/discover`, the `_meta` envelope and Tasks |
| Tools | the four run tools of `control/mcp_tools.py`, unchanged | the tool table projected from the method table; session and other verbs |
| Caller | the daemon's same-user rule, as for the CLI | caller tokens, scopes, a failed-auth limit (lane CONTROL's interface; how a client gets and loses its token is not decided) |
| Entry | this module entry | a registered `recursion mcp` command |
| Checked against | scripted clients in the tests and the fixtures below | a real MCP client product, or a conformance tool: none was run |

## Starting

The process takes no arguments. Before it reads anything from stdin it runs the socket check
(`socket_check.py`): the platform has the Unix transport; the socket path is absolute; its folder
exists, is a real folder of this user with mode 0700 or narrower; the socket exists, is a socket of
this user with mode 0600 or narrower; and one read (`run.list`, `limit: 1`) gets the control API's
answer, which also proves the listener runs as this user and admits this process (a process inside a
run unit is not admitted). If any step fails it writes one JSON line to stderr,
`{"code": "socket_check_failed", "reason": ..., "detail": ...}`, nothing to stdout, and exits 1. The
folder and socket steps are repeated before every forwarded call.

Exit statuses: 0 when the client closes stdin; 1 when the socket check fails; 2 for an argument; 130
when interrupted. Closing stdin never stops a run: calls in flight finish and are answered, and the
runs go on.

## The wire

`server.py` says what each method answers. In short: `initialize` always answers `2025-06-18`;
`server/discover` (a dual-era client's probe) and every other unknown method answer `-32601` and the
process keeps serving, so such a client falls back to `initialize`; `tools/list` and `tools/call` are
refused until `initialize`; invalid arguments are `-32602` and reach nothing; `notifications/cancelled`
drops that request's answer and sends nothing to the daemon. Diagnostics go to stderr as JSON lines;
stdout carries only protocol messages, one ASCII line each.

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
