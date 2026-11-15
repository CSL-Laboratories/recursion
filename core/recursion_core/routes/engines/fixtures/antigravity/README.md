Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.

# Antigravity fixtures, the shared ACP client prototype, and their tests

This folder is lane ENGINE-E5's (`ownership.json`: `routes/engines/fixtures/antigravity/` in every phase).
For now it holds more than fixtures, by the Liaison's ruling of Nov 14, 2026 (the lane's START.md): the
paths the brief names for the ACP client (`routes/acp.py`, `routes/tests/test_acp.py`,
`routes/fixtures/acp/`) and for engine tests (`routes/engines/tests/`) are not this lane's yet.

| File | What it is | Moves to, when the map opens the path |
|---|---|---|
| `acp.py` | the shared ACP client, engine-neutral | `routes/acp.py`, unchanged |
| `fake_acp_agent.py` | a scripted fake ACP agent (SYNTHETIC) | `routes/fixtures/acp/` |
| `acp_meta_v1.json` | the pinned ACP method list, byte for byte | with `acp.py`'s tests |
| `acp_schema_excerpt.json` | values copied by a script from the pinned ACP schema | with `acp.py`'s tests |
| `schema_check.py` | loads SPINE's `conformance/rules.py` by path to check drafts against SPINE's schemas | with the tests |
| `test_antigravity_acp.py` | the client against the fake agent | `routes/tests/test_acp.py` |
| `test_antigravity_one_shot.py` | the one-shot parser over the fixtures below, and the one-shot adapter | `routes/engines/tests/` |
| `test_antigravity_driver.py` | methods, driver.v2 declarations, the ACP glue, nothing registered | `routes/engines/tests/` |
| `one_shot/*.jsonl` | one-shot stdout streams (SYNTHETIC) | stays |

There is no `__init__.py` here or in `routes/engines/fixtures/` (that one would be lane DRIVER-CC's): the
modules import by dotted name as a namespace package. Run the tests by module name, from the repository
root:

    PYTHONPATH=core <python> -m unittest \
        recursion_core.routes.engines.fixtures.antigravity.test_antigravity_acp \
        recursion_core.routes.engines.fixtures.antigravity.test_antigravity_one_shot \
        recursion_core.routes.engines.fixtures.antigravity.test_antigravity_driver

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
    asked about an error answer to a prompt only; called with no lock held (it may call the connection);
    one that raises counts as "no limit". The default maps nothing.

class AcpConnection(*, hooks=NO_HOOKS, clock=None, permission_timeout_s=None)   # no I/O, no thread
    initialize() / authenticate(method_id) / new_session(cwd) / load_session(session_id, cwd)
    prompt(text, deadline_at) -> (rpc_id, line)
    cancel() -> [lines]; answer_permission(token, 'approve'|'deny') -> line | None; expire(now) -> [lines]
    feed(line) -> Fed(updates, asks, replies, ended); end_of_output(); deadline(); release(rpc_id)
    next_deadline(); ask(token); pending_result(rpc_id); turn_in_progress
    counters: stray_responses, stray_updates, late_answers, hook_failures

class AcpSession(process, clock, *, hooks, permission_timeout_s, on_line, on_update, on_ask)
    open(method_id, cwd, deadline_at, *, load_session_id=None) -> session id      (raises AcpError)
    prompt(text, deadline_at) -> TurnOutcome; cancel(); answer_permission(token, decision) -> bool; close()
    input_lost: True once a write to the agent failed

TurnOutcome(turn, code, stop_reason, cancel_requested, cancel_confirmed, late_stop_reason, error, limit,
            detail, text)
PermissionAsk(token, turn, tool_call, options, deadline_at, answer, answered_by, option_id)
AcpError(code, detail) > AcpRefused > AcpUnsupported; AcpProtocolError; AcpStateError; RpcError(code, message, data)
```

Codes: every `AcpError` carries a route contract code (`launch_refused`, `auth_failed`, `engine_error`,
`engine_exited`, `deadline_exceeded`, `protocol_error`). `TurnOutcome.code` is None, a route contract code,
or `cancelled`, which the route contract does not have: a caller on it maps `cancelled` itself. ACP's
`unsupported` here is not driver.v2's refusal record (that needs `call` and `flag`): a driver wraps it.
`PermissionAsk.token` is the client's own id, not an approval.v1 id; the approval queue's binding is not
here. `close()` only ends the agent's input: no fencing, joining or restarting of a worker is offered.

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
