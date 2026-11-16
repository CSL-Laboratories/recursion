Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.

# Antigravity fixtures, the shared ACP client prototype, and their tests

This folder is lane ENGINE-E5's (`ownership.json`: `routes/engines/fixtures/antigravity/` in every phase).
For now it holds more than fixtures, by the Liaison's ruling of Nov 14, 2026 (the lane's START.md): the
paths the brief names for the ACP client (`routes/acp.py`, `routes/tests/test_acp.py`,
`routes/fixtures/acp/`) and for engine tests (`routes/engines/tests/`) are not this lane's yet.

| File | What it is | Moves to, when the map opens the path |
|---|---|---|
| `acp.py` | the shared ACP client, engine-neutral | `routes/acp.py`, unchanged |
| `fake_acp_agent.py` | a scripted fake ACP agent (SYNTHETIC), in process or as a stdio process (`main`) | `routes/fixtures/acp/` |
| `acp_meta_v1.json` | the pinned ACP method list, byte for byte | with `acp.py`'s tests |
| `acp_schema_excerpt.json` | values copied by a script from the pinned ACP schema | with `acp.py`'s tests |
| `schema_check.py` | loads SPINE's `conformance/rules.py` by path to check drafts against SPINE's schemas | with the tests |
| `test_antigravity_acp.py` | the client against the fake agent | `routes/tests/test_acp.py` |
| `test_antigravity_acp_pipe.py` | the client over a real pipe, the fake running as its own process | `routes/tests/` |
| `test_antigravity_one_shot.py` | the one-shot parser over the fixtures below, and the one-shot adapter | `routes/engines/tests/` |
| `test_antigravity_driver.py` | methods, driver.v2 declarations, the ACP glue, nothing registered | `routes/engines/tests/` |
| `test_antigravity_turns.py` | the turn invariant of `AcpDrafts` over thousands of seeded turns, the real session path | `routes/engines/tests/` |
| `one_shot/*.jsonl` | one-shot stdout streams (SYNTHETIC) | stays |

There is no `__init__.py` here or in `routes/engines/fixtures/` (that one would be lane DRIVER-CC's): the
modules import by dotted name as a namespace package. Run the tests by module name, from the repository
root:

    PYTHONPATH=core <python> -m unittest \
        recursion_core.routes.engines.fixtures.antigravity.test_antigravity_acp \
        recursion_core.routes.engines.fixtures.antigravity.test_antigravity_acp_pipe \
        recursion_core.routes.engines.fixtures.antigravity.test_antigravity_one_shot \
        recursion_core.routes.engines.fixtures.antigravity.test_antigravity_driver         recursion_core.routes.engines.fixtures.antigravity.test_antigravity_turns

`unittest discover -s core` does not find them (it needs an `__init__.py` in every folder on the way); the
hosted gate does (it runs every `core/**/test_*.py` by path).

## The shared ACP client's interface (for ENGINE-E4, E6, E7 and E9)

A prototype: it is importable today only as
`recursion_core.routes.engines.fixtures.antigravity.acp`, and moves to `routes.acp` unchanged. Its rules are
in the module's docstring; its surface is this:

```
PROTOCOL_VERSION = 1; SCHEMA_PIN (repository, tag v1.18.0, commit 13b76582, schema 1.32.0, both sha256s)
INITIALIZE AUTHENTICATE SESSION_NEW SESSION_LOAD SESSION_PROMPT SESSION_CANCEL     (sent)
SESSION_UPDATE REQUEST_PERMISSION REFUSED_CLIENT_METHODS (fs/*, terminal/*, elicitation/*: answered -32601)
STOP_REASONS OPTION_KINDS METHOD_NOT_FOUND AUTH_REQUIRED CANCELLED APPROVE DENY

class EngineHooks: limit(method, RpcError) -> limit signal | None
    asked about an error answer to a prompt only; called with no lock held (it may call the connection).
    One that raises an Exception, or returns anything but None or a limit signal of contract.limit_signal's
    exact shape, counts as "no limit" (hook_failures). An interruption (KeyboardInterrupt, SystemExit) is
    never swallowed: it reaches the caller. The default maps nothing.

class AcpConnection(*, hooks=NO_HOOKS, clock=None, permission_timeout_s=None)   # no I/O, no thread
    initialize() / authenticate(method_id) / new_session(cwd) / load_session(session_id, cwd)
    prompt(text, deadline_at) -> (rpc_id, line)                 deadline_at: a finite number, else AcpInvalid
    cancel() -> [lines]; answer_permission(token, 'approve'|'deny') -> line | None; expire(now) -> [lines]
    feed(line) -> Fed(updates, asks, replies, ended); release(rpc_id)
    end_of_output(); input_closed()       the agent is gone: a turn in progress ends engine_exited
    note_input_lost()                     a write failed: a prompt's answer taken after this ends engine_exited
    abandon(code, detail, *, rpc_id=None); deadline()      end the turn here, its prompt still unanswered
    abandoned_turn; next_deadline(); ask(token); pending_result(rpc_id); turn_in_progress
    counters: stray_responses, stray_updates, late_answers, hook_failures

class AcpSession(process, clock, *, hooks=NO_HOOKS, permission_timeout_s=None, on_line=None, on_update=None,
                 on_ask=None, on_turn_end=None, on_turn_start=None)
    on_turn_start(key)                        the turn whose prompt is written, before any line of it is read
    on_turn_end(key, outcome, code, detail)   every exit of prompt, once; outcome None when prompt raised;
        key = (connection.serial, turn number), None only when no turn began (a prompt refused before its
        request); it is kept through any failure after the request (a bad answer, on_line raising, an
        interrupted write). An Exception out of it is counted (turn_end_failures), never raised. Callbacks
        bound to one object with turn_end / turn_start methods bring those along (two such objects: refused)
    open(method_id, cwd, deadline_at, *, load_session_id=None) -> session id
    prompt(text, deadline_at) -> TurnOutcome; cancel(); close()
    answer_permission(token, decision) -> bool    True only when the agent was sent the option the decision
        names (allow_once / reject_once); False when that option was not offered (it was sent `cancelled`,
        ask.option_id is None), the ask was already answered or late, or the input is gone
    input_lost: True once a write to the agent failed (send raised anything)
    One caller reads the output at a time: open or prompt while another runs raises AcpStateError at once.

TurnOutcome(turn, code, stop_reason, cancel_requested, cancel_confirmed, late_stop_reason, error, limit,
            detail, text, session)                      session (required): the connection's serial
PermissionAsk(token, turn, tool_call, options, deadline_at, answer, answered_by, option_id)
ToolCalls(digest, mask=None)       one turn's tool_call / tool_call_update updates as driver.v2 drafts
    update(params) -> [drafts]; asked(PermissionAsk); turn_ended(stopped) -> [drafts]; has_open; ignored
    digest(value) -> 64 lowercase hex (RFC 8785 text, then SHA-256): raises NoCanonicalText (or returns None)
        for a value with no canonical text (the agent's: AcpProtocolError); anything else it raises, or a
        bad digest, is the caller's (AcpInvalid). mask(text) -> text: result texts, after the join and again
        after a lone surrogate is replaced, and the call id a draft carries (a call is known by the agent's
        own id; two calls never share a draft id)
NoCanonicalText(ValueError)
AcpError(code, detail) > AcpRefused > AcpUnsupported, AcpInvalid (also ValueError), AcpStateError (also
    RuntimeError); AcpError > AcpProtocolError; RpcError(code, message, data)
```

What each call raises: an argument the caller got wrong is `AcpInvalid` (`request_invalid`), checked before
anything is written (`open` checks all of its arguments first); a call the state does not allow is
`AcpStateError` (`request_invalid`, or `engine_exited` once the agent is gone); a failure of the run is an
`AcpError` with its code. So one `except AcpError` catches every refusal and failure of the client.
`answer_permission` raises `KeyError` for a str token this connection never issued; a token that is not a
str, or a request id that is not an int (`ask`, `release`, `pending_result`), is `AcpInvalid`. A deadline, a permission
timeout or a limit's `reset_at` must be a finite float-sized number: NaN, an infinity, or an integer too
large for a float is refused (`AcpInvalid`), or counts as a bad hook answer. A failure while a turn's lines
are handled (a protocol error, or `on_line`, `on_update` or `on_ask` raising, an interruption included) is
raised by `prompt` and abandons the turn: the agent is asked to cancel it, and the next `prompt` first reads
to the agent's answer to the abandoned prompt (those lines go to `on_line` only), so a long-lived worker
keeps working. A line `on_line` raised on, even with an interruption, is still read by the connection. A
write that fails, on any thread and whatever `send` raised, loses the agent's input: the turn ends
`engine_exited` before anything more is read or taken, and every later call is refused `engine_exited`.
The session tells the connection at once (`note_input_lost`, under the connection's lock), so a prompt's
answer the connection takes after the failure can never be a success, however the threads interleave; an
interrupted write while opening closes the connection before the interruption is raised. `feed` takes a
str, and `expire` a finite `now`; anything else is `AcpInvalid`.

A SELECTED CREDENTIAL never reaches a draft or a result. The engine that holds one (an API-key route's key)
passes it to the draft makers: Antigravity's `OneShotParser(..., redact=(key,))` and
`AcpDrafts(redact=(key,))` mask it in every spelling JSON allows (as it stands, as a serializer escapes it, and
with any character written as an escape), with a fill of the same UTF-8 length (asterisks, or `#` for a value
made only of asterisks), so the byte offsets of `engine.output` still match a capture masked the same way
(`mask(line, mask_forms(values))`). A decoded one-shot line that still holds the value fails closed. Antigravity's one-shot
adapter launches a keyed route only through an `output_boundary` (a launcher, asked for by the values to
keep out, whose capture masks them before anything is persisted) and refuses it without one, before custody
is asked (`antigravity_output_boundary_missing`). E4, E6, E7 and E9 need the same for their keyed routes.
`AcpDrafts.ended` takes `turn_id` (`turn-...`) and `delivery_id` (`dlv-...`) of common.v1's form, both or
neither. `open_acp(..., drafts=d)` wires all five of its callbacks, the session's `on_turn_start` and
`on_turn_end` included (and wiring `d`'s own callbacks brings `d.turn_start` and `d.turn_end` along). One
`AcpDrafts` serves sessions one thread at a time; a session run inside another's callback is nested, and a
call from another thread during an announced turn is refused (`AcpStateError`). `AcpDrafts(redact=...)`
also masks a value split across a line's or an update's pieces, and refuses what cannot be masked in place. The session tells `on_turn_end` every end of a turn `prompt`
began, once, so one place in the drafts owns a turn's open tool calls whatever ends the turn; a second end
drafts nothing and raises nothing.
A permission answer is decided when it claims the ask, against a clock read after the claim, so a wait for
a lock never lets a late answer approve. The deadline of a step that starts work is checked once the step
holds the session's ordering lock.

The ACP drive as a route adapter: `AntigravityAcpAdapter` (and `acp_factory`, `binary` only) runs one turn of `agy acp` per run, with the one-shot adapter's STRICT environment path shared and unchanged. A run needs an absolute `cwd` in the request (`request_invalid` otherwise), and that folder, with no fallback, is what the agent is told (`session/new`); the run's folder is the workspace lane's to decide. The adapter does not decide where the process starts: it hands the launcher no working directory (the launcher contract has none), so once the daemon can launch this engine (its launcher has no `antigravity` profile yet) the child starts in the engine's home under the daemon's state folder. It must not be registered until the launch boundary starts the child in the run's folder, and `acp_factory` refuses until then. The class itself is registrable (the engine table checks only that a factory is callable), and an adapter built that way refuses every run before any launch (`launch_refused`, `antigravity_acp_launch_has_no_run_folder`) until the launch boundary and the agent's folder are one folder. Nothing registers it. Its tests: `test_antigravity_acp_adapter.py`.

Codes: every `AcpError` carries a route contract code (`launch_refused`, `auth_failed`, `engine_error`,
`engine_exited`, `deadline_exceeded`, `protocol_error`, `request_invalid`). `TurnOutcome.code` is None, a
route contract code, or `cancelled`, which the route contract does not have: a caller on it maps
`cancelled` itself. ACP's
`unsupported` here is not driver.v2's refusal record (that needs `call` and `flag`): a driver wraps it.
`PermissionAsk.token` is the client's own id, not an approval.v1 id; the approval queue's binding is not
here. `close()` only ends the agent's input: no fencing, joining or restarting of a worker is offered.

A consumer, end to end (runnable as it stands; a test runs it):

```python
from recursion_core.routes.engines import antigravity as ag
from recursion_core.routes.engines.fixtures.antigravity import acp
from recursion_core.routes.engines.fixtures.antigravity.fake_acp_agent import FakeAcpAgent

agent = FakeAcpAgent([[("update", {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "run",
                                   "kind": "execute", "status": "completed", "rawInput": {"n": 1}}),
                       ("end", "end_turn")]])
route = {"engine": "antigravity", "model": "made-up", "auth_method": "gemini-api-key",
         "connection": {"kind": "api_key", "id": "conn-A", "credential_env": "GEMINI_API_KEY"}}
drafts = ag.AcpDrafts(redact=("made-up-key",))
session = ag.open_acp(agent, agent.clock, route, "/var/tmp/workspace", agent.clock() + 60, drafts=drafts)
outcome = session.prompt("hello", agent.clock() + 60)
assert (outcome.session, outcome.turn) == (session.connection.serial, 1)
drafts.ended(outcome, "turn-1", "dlv-1")
kinds = [d["type"] for d in drafts.drafts if d["type"] != "engine.output"]
assert kinds == ["tool.call", "tool.result", "input.accepted", "result"], kinds

tools = acp.ToolCalls(ag.input_digest)          # the client alone, under another engine's draft maker
assert [d["type"] for d in tools.update({"update": {"sessionUpdate": "tool_call", "toolCallId": "x",
                                                    "status": "completed"}})] == ["tool.call", "tool.result"]
```

## The ACP pin

`agentclientprotocol/agent-client-protocol`, tag `v1.18.0`, commit
`13b765828f961812f0c78ae577220e994209fa2f`; `schema/v1/schema.json` (schema version 1.32.0), sha256
`e747a7bec3c02708b70885bf3cccac5c72fa421390dd7e85758fd61e33b1ae21`; `schema/v1/meta.json`, sha256
`061edb6efa8fb2aa2792459a86ec7268de5fe665bba48b2ffe7939df01481f88` (kept here as `acp_meta_v1.json`).
Fetched anonymously on Nov 14, 2026. The whole schema file is not kept; `acp_schema_excerpt.json` holds the
values the client relies on, copied by a script, with the schema's sha256.

## Every fixture is SYNTHETIC

Nothing here was captured from Antigravity or any other agent: no lane runs an engine binary or signs in.

- `fake_acp_agent.py` is written by hand from the pinned schema and from the agent behaviour the vault
  records describe (20261030-csl496-f9-acp-adapter-wire-01, 20261030-csl496-d3c-gemini-opencode-acp-cancel-01).
- The one-shot streams follow the event shapes on Google's "Headless mode" page for the Antigravity CLI
  (`https://antigravity.google/docs/cli/headless`), read on Nov 14, 2026 through a fetch tool that may
  summarise: one `init` event, `step_update` events (`step_index`, `state` ACTIVE or DONE, `step_type`
  `user_input`, `agent_response`, `tool` or `checkpoint`, `tool_info` with `name`, `parameters`, `output`
  and an `error` object with `type` and `message`), and one `result` event with `status` (SUCCESS, ERROR,
  CANCELED, INTERRUPTED, INVALID, WAITING, RUNNING), `response`, `error`, `num_turns` and `usage`
  (`input_tokens`, `output_tokens`, `thinking_tokens`, `cache_read_tokens`, `total_tokens`). The texts,
  ids, numbers and error messages are made up. The conversation id is a made-up UUID.

| Fixture | Case |
|---|---|
| `synthetic-ok.jsonl` | a successful run with per-step usage and a result |
| `synthetic-tool.jsonl` | one tool step, ACTIVE then DONE with `tool_info` |
| `synthetic-tool-error.jsonl` | a tool step whose `tool_info` carries an `error` object |
| `synthetic-error-unknown-model.jsonl` | a run that fails before any turn (status ERROR, zero usage, no conversation id), shaped after the page's unknown-model example |
| `synthetic-error-quota.jsonl` | status ERROR whose made-up `error` text sounds like a quota |
| `synthetic-interrupted.jsonl` | status INTERRUPTED |
| `synthetic-no-result.jsonl` | the stream ends with no result |
| `synthetic-unknown-event.jsonl` | an event and a field the parser does not know |
| `synthetic-model-text-sounds-like-quota.jsonl` | a successful run whose answer text talks about a rate limit |

The headless page's own examples show `total_tokens` equal to `input_tokens + output_tokens`, so the
parser reads `thinking_tokens` as the part of `output_tokens` that is reasoning (event.v1's
`reasoning_tokens`) only when it is not larger than `output_tokens`. That is an inference from two
examples, not a documented rule.
